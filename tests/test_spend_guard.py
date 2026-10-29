"""Regression tests for the one spend path: limits under concurrency, pause and breakers, in-flight
/panic and /dry on, stable rules.json ids, the one-time go-live button, unknown buy results."""

import asyncio
import json
from dataclasses import replace

from helpers import ADMIN, NOW, Clock, L, demo_engine, settings

from mrkt_autobuy import doctor
from mrkt_autobuy.bot import AppContext, dry_off_gate, go_live_button
from mrkt_autobuy.callbacks import LiveCB
from mrkt_autobuy.client import DEMO_RULES, ApiError, BuyResult, MockClient
from mrkt_autobuy.config import Settings
from mrkt_autobuy.events import EventLog, read_events
from mrkt_autobuy.ledger import rule_verdicts
from mrkt_autobuy.peers import Peers, TgValueService
from mrkt_autobuy.pricing import FeeModel
from mrkt_autobuy.rules import BuyRule, load_rules_file
from mrkt_autobuy.storage import Storage

CONFIRM_RULES = [replace(r, mode="confirm") if r.mode == "auto" else r for r in DEMO_RULES]


class SlowClient(MockClient):
    """Network latency on balance() and buy(), so concurrent callers really interleave; a hook in balance()."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.on_balance = None

    async def balance(self):
        if self.on_balance:
            hook, self.on_balance = self.on_balance, None
            await hook()
        await asyncio.sleep(0.02)
        return 1000.0

    async def buy(self, listing):
        await asyncio.sleep(0.02)
        return await super().buy(listing)


async def live_confirm_cards(**over):
    """LIVE armed, every demo rule in confirm mode, one cycle run: {listing_id: token} of the pending cards."""
    clock = Clock()
    engine, storage, client, notifier, clock = await demo_engine(
        settings(DRY_RUN=False, **over), rules=CONFIRM_RULES, force_dry=False, clock=clock,
        client=SlowClient("demo", now=clock()))
    assert await engine.arm_live() is None  # before the cycle: the cards are rendered LIVE
    await engine.run_cycle()
    cards = {c.listing_id: c.token for c in await storage.list_pending(("pending",))}
    return engine, storage, client, notifier, clock, cards


# --- high: the limits hold under concurrency ------------------------------------------------------------


def test_concurrent_taps_cannot_break_the_hourly_cap():
    async def go():
        engine, storage, client, _, clock, cards = await live_confirm_cards(MAX_BUYS_PER_HOUR=1)
        try:
            res = await asyncio.gather(*(engine.tap("b", cards[k], ADMIN) for k in ("dc-3002", "hc-1234")))
            assert len(client.bought) == 1
            assert await storage.buys_since(clock() - 3600, False) == 1
            assert sum(r.ok for r in res) == 1 and any("MAX_BUYS_PER_HOUR 1 reached" in r.text for r in res)
        finally:
            await storage.close()

    asyncio.run(go())


def test_concurrent_taps_cannot_break_the_daily_budget():
    async def go():
        engine, storage, client, _, clock, cards = await live_confirm_cards(DAILY_BUDGET_TON=6, CONFIRM_ABOVE_TON=100)
        try:
            toks = [cards[k] for k in ("dc-3002", "hc-1234", "sa-777")]
            res = await asyncio.gather(*(engine.tap("b", t, ADMIN) for t in toks))
            assert await storage.spent_since(clock() - 86400, False) <= 6 + 1e-9
            assert len(client.bought) == 2
            assert any("DAILY_BUDGET_TON 6" in r.text for r in res)
        finally:
            await storage.close()

    asyncio.run(go())


def test_auto_buy_and_tap_at_once_respect_the_hourly_cap():
    async def go():
        clock = Clock()
        engine, storage, client, _, clock = await demo_engine(
            settings(DRY_RUN=False, MAX_BUYS_PER_HOUR=4), force_dry=False, clock=clock,
            client=SlowClient("demo", now=clock()))
        try:
            await engine.arm_live()
            await engine.run_cycle()  # 3 live auto-buys, the confirm card for dc-3002 waits
            assert len(client.bought) == 3
            token = next(c.token for c in await storage.list_pending(("pending",)) if c.listing_id == "dc-3002")
            client.advance()
            clock.advance(5)
            await asyncio.gather(engine.run_cycle(), engine.tap("b", token, ADMIN))  # ls-4417 auto + the tap
            assert await storage.buys_since(clock() - 3600, False) == 4
            assert len(client.bought) == 4
        finally:
            await storage.close()

    asyncio.run(go())


# --- high: pause and breakers really stop buying ----------------------------------------------------


class Drained(SlowClient):
    async def balance(self):
        return 900.0  # 100 TON gone that we never spent


async def _drained_engine():
    clock = Clock()
    engine, storage, client, notifier, clock = await demo_engine(
        settings(DRY_RUN=False, MAX_BUYS_PER_HOUR=10), force_dry=False, clock=clock, client=Drained("demo", now=clock()))
    await engine.start_in_practice()
    return engine, storage, client, notifier, clock


async def _trip_balance_breaker_next_cycle(engine, storage, clock):
    await engine.arm_live()
    await storage.set_setting("balance_last", 1000.0)
    await storage.set_setting("balance_ts", clock() - 60)


def test_balance_breaker_stops_the_same_cycle():
    async def go():
        engine, storage, client, notifier, clock = await _drained_engine()
        try:
            await _trip_balance_breaker_next_cycle(engine, storage, clock)
            await engine.run_cycle()  # three live auto matches: ls-4410 trips the breaker, the rest must not buy
            assert await engine.is_paused()
            assert any("someone else may be using this account. Paused." in m for m in notifier.messages)
            assert client.bought == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_balance_breaker_expires_pending_cards():
    async def go():
        engine, storage, client, notifier, clock = await _drained_engine()
        try:
            await engine.run_cycle()  # practice: the confirm card for dc-3002 is pending
            [card] = [c for c in await storage.list_pending(("pending",)) if c.listing_id == "dc-3002"]
            await _trip_balance_breaker_next_cycle(engine, storage, clock)
            client.advance()
            clock.advance(5)
            await engine.run_cycle()  # ls-4417 trips the breaker
            assert await engine.is_paused() and client.bought == []
            assert await storage.list_pending(("pending",)) == []  # Buy buttons stop working at once
            await engine.resume()
            res = await engine.tap("b", card.token, ADMIN)  # even after /resume the old card stays dead
            assert not res.ok and res.text == "Expired or already handled" and client.bought == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_failed_buys_breaker_stops_the_same_cycle():
    class Failing(MockClient):
        async def buy(self, listing):
            self.bought.append(listing.id)
            return BuyResult(False, "500 server error")

    async def go():
        clock = Clock()
        engine, storage, client, notifier, clock = await demo_engine(
            settings(DRY_RUN=False, MAX_BUYS_PER_HOUR=10, AUTO_PAUSE_ERRORS=1), force_dry=False, clock=clock,
            client=Failing("demo", now=clock()))
        try:
            await engine.arm_live()
            await engine.run_cycle()
            assert await engine.is_paused() and client.bought == ["ls-4410"]  # one attempt, then stop
        finally:
            await storage.close()

    asyncio.run(go())


def test_taps_refused_while_paused_or_rule_paused():
    async def go():
        engine, storage, client, _, clock, cards = await live_confirm_cards()
        try:
            await engine.set_paused(True)  # /pause, or a breaker
            res = await engine.tap("b", cards["dc-3002"], ADMIN)
            assert not res.ok and "Paused" in res.text and client.bought == []
            await engine.set_paused(False)

            card = await storage.get_pending(cards["dc-3002"])
            await storage.set_setting(f"rule_paused_until:{card.rule_id}", clock() + 3600)  # crash breaker
            res = await engine.tap("b", cards["dc-3002"], ADMIN)
            assert not res.ok and "floor-crash breaker" in res.text and client.bought == []

            await storage.delete_setting(f"rule_paused_until:{card.rule_id}")
            await storage.update_rule(card.rule_id, state="paused")
            res = await engine.tap("b", cards["dc-3002"], ADMIN)
            assert not res.ok and f"rule #{card.rule_id} is paused" in res.text and client.bought == []

            await storage.update_rule(card.rule_id, state="live")
            res = await engine.tap("b", cards["dc-3002"], ADMIN)  # the card stayed pending: it still works
            assert res.ok and client.bought == ["dc-3002"]
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: /panic and /dry on during an in-flight live buy -------------------------------------------


def test_panic_or_dry_on_during_the_balance_call_stops_the_buy():
    for name in ("panic", "dry on"):
        async def go():
            engine, storage, client, _, _, cards = await live_confirm_cards()
            try:
                async def switch():
                    await (engine.panic() if name == "panic" else engine.set_dry(True))

                client.on_balance = switch
                res = await engine.tap("b", cards["dc-3002"], ADMIN)
                assert not res.ok and client.bought == [], name
                assert "BOUGHT" not in res.text
                assert [p for p in await storage.recent_purchases() if not p.dry] == []
            finally:
                await storage.close()

        asyncio.run(go())


def test_dry_on_during_peer_lookup_turns_an_auto_buy_into_practice():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            await engine.arm_live()
            original = engine._peer_view

            async def slow_view(*a, **kw):
                await engine.set_dry(True)  # /dry on lands while the engine waits for the second opinion
                return await original(*a, **kw)

            engine._peer_view = slow_view
            report = await engine.run_cycle()
            assert client.bought == [] and report.bought == 0 and report.dry_bought >= 1
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: rules.json keeps rule ids across restarts ---------------------------------------------------


def test_file_rules_keep_their_ids_and_per_rule_state_across_restarts(tmp_path):
    s = settings()

    async def go():
        storage = await Storage(str(tmp_path / "db.sqlite3")).open()
        try:
            await storage.add_rule(BuyRule("Bot Rule", 1.0, source="bot"))
            await storage.replace_rules_from_source("file", [BuyRule("Lunar Snake", 3.0),
                                                             BuyRule("Desk Calendar", 2.0, model="Classic")])
            snake, calendar = [r for r in await storage.list_rules() if r.source == "file"]
            for i in range(6):
                pid = await storage.record_purchase(L(f"x{i}", 1.0), floor_ton=1.5, rule_id=snake.id, dry=True,
                                                    status="dry", cost_ton=1.0, created_at=NOW - 3600 * (i + 1))
                await storage.add_mark(pid, NOW - 60, 1.5, 1.4, "high")
            await storage.set_setting(f"rule_paused_until:{snake.id}", NOW + 3600)
            fees = FeeModel(0, 5, 0)
            assert (await rule_verdicts(storage, [snake], s, NOW, fees))[snake.id] == "ready"

            # restart: same file, then an edited max, then the calendar rule removed and a new one added
            await storage.replace_rules_from_source("file", [BuyRule("Lunar Snake", 3.0),
                                                             BuyRule("Desk Calendar", 2.0, model="Classic")])
            await storage.replace_rules_from_source("file", [BuyRule("Lunar Snake", 4.0), BuyRule("Jelly Bunny", 5.0)])
            rules = await storage.list_rules()
            by_name = {r.collection: r for r in rules}
            assert by_name["Lunar Snake"].id == snake.id and by_name["Lunar Snake"].max_price_ton == 4.0
            assert "Desk Calendar" not in by_name and by_name["Jelly Bunny"].id not in (snake.id, calendar.id)
            assert by_name["Bot Rule"].source == "bot"  # rules made in the bot are untouched
            assert (await rule_verdicts(storage, [by_name["Lunar Snake"]], s, NOW, fees))[snake.id] == "ready"
            assert await storage.get_float(f"rule_paused_until:{snake.id}") == NOW + 3600
        finally:
            await storage.close()

    asyncio.run(go())


def test_repeated_filters_in_rules_json_keep_their_own_ids(tmp_path):
    path = tmp_path / "rules.json"
    path.write_text(json.dumps([{"collection": "Lunar Snake", "max": 2, "mode": "auto"},
                                {"collection": "Lunar Snake", "max": 3, "mode": "confirm"}]))

    async def go():
        storage = await Storage(":memory:").open()
        try:
            await storage.replace_rules_from_source("file", load_rules_file(path))
            first = [(r.id, r.mode) for r in await storage.list_rules()]
            await storage.replace_rules_from_source("file", load_rules_file(path))
            assert [(r.id, r.mode) for r in await storage.list_rules()] == first == [(1, "auto"), (2, "confirm")]
        finally:
            await storage.close()

    asyncio.run(go())


# --- low: the go-live button is one-time and dies with /panic -----------------------------------------


async def _ready_ctx():
    engine, storage, client, notifier, clock = await demo_engine(
        settings(DRY_RUN=False), rules=[BuyRule("Lunar Snake", 3.0, source="bot")], force_dry=False)
    for i in range(6):
        pid = await storage.record_purchase(L(f"x{i}", 1.0), floor_ton=1.5, rule_id=1, dry=True, status="dry",
                                            cost_ton=1.0, created_at=clock() - 3600 * (i + 1))
        await storage.add_mark(pid, clock() - 60, 1.5, 1.4, "high")
    engine.last_mark_at = clock()  # the report does not re-mark (no search in tests)
    return AppContext(engine.settings, storage, client, engine), engine, storage


def _live_nonce(kb) -> str:
    [button] = [b for row in kb.inline_keyboard for b in row if b.text.startswith("Go live")]
    return LiveCB.unpack(button.callback_data).nonce


def test_stale_go_live_button_cannot_arm_live_after_panic_and_resume():
    async def go():
        ctx, engine, storage = await _ready_ctx()
        try:
            _, kb = await dry_off_gate(ctx, confirm=False)
            old = _live_nonce(kb)
            await engine.panic()
            await engine.resume()
            assert await go_live_button(ctx, old) is not None
            assert await engine.is_dry()  # /resume must be followed by a new /dry off

            _, kb = await dry_off_gate(ctx, confirm=False)
            fresh = _live_nonce(kb)
            assert await go_live_button(ctx, fresh) is None and not await engine.is_dry()
            await engine.set_dry(True)
            assert await go_live_button(ctx, fresh) is not None and await engine.is_dry()  # one-time
        finally:
            await storage.close()

    asyncio.run(go())


# --- low: a buy with an unknown result counts toward the limits -----------------------------------------


def test_buy_with_unknown_result_counts_toward_limits():
    class Timeout(MockClient):
        async def buy(self, listing):
            self.bought.append(listing.id)
            raise ApiError(0, "network error: ReadTimeout")

    async def go():
        clock = Clock()
        engine, storage, client, notifier, clock = await demo_engine(
            settings(DRY_RUN=False, MAX_BUYS_PER_HOUR=1, AUTO_PAUSE_ERRORS=5), force_dry=False, clock=clock,
            client=Timeout("demo", now=clock()))
        try:
            await engine.arm_live()
            await engine.run_cycle()
            assert client.bought == ["ls-4410"]  # the unknown buy used the hour's only slot
            [p] = [p for p in await storage.recent_purchases() if not p.dry]
            assert p.status == "unknown" and await storage.buys_since(clock() - 3600, False) == 1
            assert await storage.spent_since(clock() - 86400, False) == p.cost_ton
        finally:
            await storage.close()

    asyncio.run(go())


# --- low: events, peers budget, /doctor probe ------------------------------------------------------------


def test_skip_events_carry_the_live_mode(tmp_path):
    async def go():
        path = tmp_path / "events.jsonl"
        engine, storage, client, notifier, clock = await demo_engine(
            settings(DRY_RUN=False, MAX_BUYS_PER_HOUR=10), force_dry=False, events=EventLog(path, 5))
        try:
            await engine.arm_live()
            await engine.run_cycle()
            skips = [e for e in read_events(path) if e["kind"] == "skip"]
            assert skips and all(e["mode"] == "live" for e in skips)
        finally:
            await storage.close()

    asyncio.run(go())


def test_peers_view_with_no_time_left_does_not_wait_for_the_network(tmp_path):
    async def hang(slug):
        await asyncio.sleep(30)

    async def go():
        peers = Peers(settings(PEER_FLOOR_FILES=""), "mrkt", tg=TgValueService(hang, clock=lambda: NOW),
                      clock=lambda: NOW)
        view = await asyncio.wait_for(peers.view(L("x", 1.0), BuyRule("Lunar Snake", 5), None, timeout=0), 1)
        assert view.tg is None and view.tg_status == "timeout"

    asyncio.run(go())


def test_doctor_probe_is_not_cached_forever(monkeypatch):
    calls = []

    async def fake_probe(settings, session=None, timeout=5.0):
        calls.append(1)
        return True, len(calls) > 1  # 2FA turned on between two /doctor runs

    import mrkt_autobuy.tg_auth as tg_auth

    monkeypatch.setattr(tg_auth, "probe_session", fake_probe)
    probe = doctor.make_tg_probe(Settings(_env_file=None, TG_API_ID=1, TG_API_HASH="x"))

    async def go():
        assert await probe() == (True, False) and await probe() == (True, False)  # one run shares one probe
        monkeypatch.setattr(doctor, "PROBE_TTL_SEC", -1.0)
        assert await probe() == (True, True)  # the next /doctor asks Telegram again

    asyncio.run(go())
    assert len(calls) == 2
