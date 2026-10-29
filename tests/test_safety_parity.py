"""Regression tests for the safety fixes shared by the three gift sniper bots: the fail-closed live gate,
practice cards after going live, unknown buy results, the write-ahead purchase row, rules edited during a
buy, shutdown, housekeeping after failed cycles, rules.json validation and shadow state, and the floor
confidence rule (two decoy listings never make a 'med' floor)."""

import asyncio

import pytest
from helpers import ADMIN, NOW, Clock, L, demo_engine, settings

from mrkt_autobuy.client import ApiError, MockClient, parse_buy_response
from mrkt_autobuy.engine import Engine
from mrkt_autobuy.fx import TonRate
from mrkt_autobuy.notifier import DealView, MemoryNotifier, render_deal_card
from mrkt_autobuy.peers import Peers, TgValueService
from mrkt_autobuy.pricing import assess, estimate_floor, fee_model_from_settings
from mrkt_autobuy.rules import BuyRule, RuleParseError, match, near_miss, parse_addrule_args, rule_from_dict
from mrkt_autobuy.storage import Storage
from test_spend_guard import CONFIRM_RULES, SlowClient


def run(coro):
    return asyncio.run(coro)


# --- is_dry() fails closed ------------------------------------------------------------------------------


def test_fresh_database_with_dry_run_false_starts_in_practice():
    async def go():
        engine, storage, client, _, _ = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            assert await engine.is_dry()  # no /dry off yet: practice, even without start_in_practice()
            report = await engine.run_cycle()
            assert report.dry_bought >= 1 and report.bought == 0 and client.bought == []
            assert all(p.dry for p in await storage.recent_purchases(50))
        finally:
            await storage.close()

    run(go())


def test_dry_flag_alone_or_gate_alone_is_not_live():
    async def go():
        engine, storage, _, _, clock = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            await storage.set_flag("dry", False)  # dry off but no live_until: no expiring gate, so practice
            assert await engine.is_dry()
            await storage.delete_setting("dry")
            await storage.set_setting("live_until", clock() + 3600)  # gate but no dry flag: practice
            assert await engine.is_dry()
        finally:
            await storage.close()

    run(go())


def test_arm_live_writes_the_gate_before_the_dry_flag():
    async def go():
        engine, storage, _, _, _ = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        real = storage.set_flag

        async def failing(key, value):
            if key == "dry" and value is False:
                raise OSError("disk full")
            await real(key, value)

        storage.set_flag = failing
        try:
            with pytest.raises(OSError):
                await engine.arm_live()
            assert await storage.get_float("live_until") is not None
            assert await engine.is_dry()  # the half-written arm stays practice
        finally:
            await storage.close()

    run(go())


# --- practice cards never spend real TON ----------------------------------------------------------------


async def _practice_cards():
    """DRY_RUN=false but not armed: every demo rule asks first, so the cards are rendered PRACTICE."""
    clock = Clock()
    engine, storage, client, notifier, clock = await demo_engine(
        settings(DRY_RUN=False), rules=CONFIRM_RULES, force_dry=False, clock=clock, client=MockClient("demo", now=clock()))
    await engine.run_cycle()
    cards = {c.listing_id: c for c in await storage.list_pending(("pending",))}
    assert cards and all(c.payload["practice"] for c in cards.values())
    return engine, storage, client, notifier, cards


def test_arming_live_expires_every_pending_practice_card():
    async def go():
        engine, storage, client, notifier, cards = await _practice_cards()
        try:
            assert await engine.arm_live() is None
            for card in cards.values():
                assert (await storage.get_pending(card.token)).status == "expired"
            assert sum("live mode started, this was a practice card" in e["note"] for e in notifier.edits) == len(cards)
            res = await engine.tap("b", cards["dc-3002"].token, ADMIN)
            assert not res.ok and res.text == "Expired or already handled" and client.bought == []
        finally:
            await storage.close()

    run(go())


def test_a_practice_card_tapped_while_live_stays_practice():
    async def go():
        engine, storage, client, _, cards = await _practice_cards()
        try:
            # LIVE armed behind the cards' back (no expiry sweep): the tap must still be a practice buy
            await storage.set_setting("live_until", engine.clock() + 3600)
            await storage.set_flag("dry", False)
            assert not await engine.is_dry()
            res = await engine.tap("b", cards["dc-3002"].token, ADMIN)
            assert res.ok and "PRACTICE" in res.text and client.bought == []
            [p] = [p for p in await storage.recent_purchases(50) if p.listing_id == "dc-3002"]
            assert p.dry and p.status == "dry"

            # a card without the flag (older payload) counts as practice too
            token = cards["hc-1234"].token
            await storage.db.execute("UPDATE pending SET payload_json=json_remove(payload_json, '$.practice') "
                                     "WHERE token=?", (token,))
            await storage.db.commit()
            assert "practice" not in (await storage.get_pending(token)).payload
            res = await engine.tap("b", token, ADMIN)
            assert res.ok and "PRACTICE" in res.text and client.bought == []
        finally:
            await storage.close()

    run(go())


# --- unknown buy results count toward the limits ----------------------------------------------------------


@pytest.mark.parametrize("body", [[], {}, {"ok": True}, {"message": "Gift purchased"}, {"error": "busy"}, None,
                                  "weird", 1])
def test_any_buy_body_but_explicit_success_is_uncertain(body):
    res = parse_buy_response(body, "x")
    assert not res.ok and res.uncertain and "counted toward your limits" in res.detail


def test_explicit_success_is_a_buy():
    res = parse_buy_response([{"userGift": {}, "price": 1}], "x")
    assert res.ok and not res.uncertain


def test_uncertain_buy_is_recorded_unknown_and_counts_toward_the_cap():
    class Odd(MockClient):
        async def buy(self, listing):
            self.bought.append(listing.id)
            return parse_buy_response({"status": "done"}, listing.id)

    async def go():
        clock = Clock()
        engine, storage, client, _, clock = await demo_engine(
            settings(DRY_RUN=False, MAX_BUYS_PER_HOUR=1, AUTO_PAUSE_ERRORS=5),
            rules=[BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="bot")],
            force_dry=False, clock=clock, client=Odd("demo", now=clock()))
        try:
            await engine.arm_live()
            await engine.run_cycle()
            [p] = await storage.recent_purchases(10)
            assert p.status == "unknown" and not p.dry
            assert await storage.buys_since(clock() - 3600, False) == 1
            client.advance()
            clock.advance(5)
            report = await engine.run_cycle()  # ls-4417 matches, but the hour's one buy is already used
            assert client.bought == ["ls-4410"] and report.skipped == 1
        finally:
            await storage.close()

    run(go())


def test_live_buy_is_recorded_unknown_before_the_request_goes_out():
    seen = []

    class Watching(MockClient):
        async def buy(self, listing):
            seen.extend((p.status, p.dry) for p in await self.storage.recent_purchases(10))
            return await super().buy(listing)

    async def go():
        clock = Clock()
        client = Watching("demo", now=clock())
        engine, storage, _, _, clock = await demo_engine(
            settings(DRY_RUN=False),
            rules=[BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="bot")],
            force_dry=False, clock=clock, client=client)
        client.storage = storage
        try:
            await engine.arm_live()
            await engine.run_cycle()
            assert seen == [("unknown", False)]  # the row existed while the request was in flight
            [p] = await storage.recent_purchases(10)
            assert p.status == "ok"
        finally:
            await storage.close()

    run(go())


def test_a_buy_cancelled_mid_request_still_counts():
    started = asyncio.Event()

    class Hanging(MockClient):
        async def buy(self, listing):
            started.set()
            await asyncio.sleep(3600)

    async def go():
        clock = Clock()
        engine, storage, client, _, clock = await demo_engine(
            settings(DRY_RUN=False),
            rules=[BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="bot")],
            force_dry=False, clock=clock, client=Hanging("demo", now=clock()))
        try:
            await engine.arm_live()
            task = asyncio.create_task(engine.run_cycle())
            await started.wait()
            task.cancel()  # shutdown lands while the buy request is in flight
            with pytest.raises(asyncio.CancelledError):
                await task
            [p] = await storage.recent_purchases(10)
            assert p.status == "unknown" and not p.dry
            assert await storage.spent_since(clock() - 86400, False) == pytest.approx(1.0)
            assert await storage.buys_since(clock() - 3600, False) == 1
        finally:
            await storage.close()

    run(go())


# --- the rule as stored right before money moves --------------------------------------------------------


@pytest.mark.parametrize("edit,code", [({"mode": "watch"}, "MODE_CHANGED"), ({"max_price_ton": 0.5}, "OVER_MAX"),
                                       ({"enabled": False}, "DISABLED")])
def test_rule_edited_during_the_balance_call_stops_the_auto_buy(edit, code):
    async def go():
        clock = Clock()
        engine, storage, client, _, clock = await demo_engine(
            settings(DRY_RUN=False),
            rules=[BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="bot")],
            force_dry=False, clock=clock, client=SlowClient("demo", now=clock()))
        try:
            await engine.arm_live()

            async def edit_rule():
                await storage.update_rule(1, **edit)

            client.on_balance = edit_rule
            await engine.run_cycle()
            assert client.bought == [] and await storage.buys_since(clock() - 3600, False) == 0
            assert (await storage.decisions_for("ls-4410"))[0].reason_code == code
        finally:
            await storage.close()

    run(go())


# --- shutdown and housekeeping --------------------------------------------------------------------------


def test_drain_lets_no_new_buy_start():
    async def go():
        engine, storage, client, _, _ = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            await engine.arm_live()
            assert await engine.drain(1.0)
            report = await engine.run_cycle()
            assert client.bought == [] and report.reasons["PAUSED"] >= 1
            assert [p for p in await storage.recent_purchases(50) if not p.dry] == []
        finally:
            await storage.close()

    run(go())


def test_housekeeping_runs_after_a_failed_cycle():
    class Down(MockClient):
        async def collection_floors(self):
            raise ApiError(503, "down")

    async def go():
        clock = Clock()
        engine, storage, client, notifier, clock = await demo_engine(clock=clock, client=Down("demo", now=clock()))
        try:
            await storage.create_pending("tok", "ls-1", 1, 1.0, 1.0, {"title": "X"}, clock() - 200, 90)
            await engine.run_forever(asyncio.Event(), max_cycles=1)
            assert engine.stats.consecutive_errors == 1
            assert (await storage.get_pending("tok")).status == "expired"
        finally:
            await storage.close()

    run(go())


# --- rules.json -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, float("nan"), float("inf"), "x"])
def test_rules_json_rejects_a_max_that_is_not_a_positive_number(bad):
    with pytest.raises(RuleParseError):
        rule_from_dict({"collection": "Lunar Snake", "max_price_ton": bad})


@pytest.mark.parametrize("field", ["min_discount_pct", "min_profit_ton", "min_profit_pct", "allow_locked_days"])
def test_rules_json_rejects_nan_and_negative_thresholds(field):
    for bad in (float("nan"), -5):
        with pytest.raises(RuleParseError):
            rule_from_dict({"collection": "Lunar Snake", "max_price_ton": 2, field: bad})


@pytest.mark.parametrize("arg", ["max=nan", "max=inf", "max=2 discount=nan", "max=2 profit=inf"])
def test_addrule_rejects_non_finite_numbers(arg):
    with pytest.raises(RuleParseError):
        parse_addrule_args(f"Lunar Snake {arg}")


def test_near_miss_skips_a_rule_with_max_zero():
    listing = L("a", 1.0)
    deal = assess(listing, estimate_floor(listing, [L(f"p{i}", 1.6 + i / 100) for i in range(5)], None, [], NOW),
                  fee_model_from_settings(settings()), [])
    assert near_miss(BuyRule("Lunar Snake", 0.0), listing, deal) is None  # no ZeroDivisionError


def test_new_file_rules_start_in_shadow_and_keep_their_stored_state():
    async def go():
        storage = await Storage(":memory:").open()
        try:
            await storage.replace_rules_from_source("file", [rule_from_dict({"collection": "Lunar Snake", "max": 2})],
                                                    new_state="shadow")
            [rule] = await storage.list_rules()
            assert rule.state == "shadow"
            await storage.update_rule(rule.id, state="live")  # /promote
            await storage.replace_rules_from_source("file", [rule_from_dict({"collection": "Lunar Snake", "max": 3})],
                                                    new_state="shadow")
            [rule] = await storage.list_rules()
            assert rule.state == "live" and rule.max_price_ton == 3  # the promotion survives a restart
            explicit = rule_from_dict({"collection": "Lunar Snake", "max": 3, "state": "paused"})
            await storage.replace_rules_from_source("file", [explicit], new_state="shadow")
            assert (await storage.list_rules())[0].state == "paused"  # only an explicit state overwrites
        finally:
            await storage.close()

    run(go())


# --- floor confidence: a cluster of at least 3 listings ---------------------------------------------------


def test_two_decoy_listings_never_make_a_med_floor():
    candidate = L("c", 1.0)
    decoys = [L("d1", 2.00), L("d2", 2.02)]
    est = estimate_floor(candidate, decoys, None, [], NOW)
    assert est.confidence == "low"
    est_k1 = estimate_floor(candidate, decoys, None, [], NOW, k=1)  # CLUSTER_K=1: a 2-listing "cluster"
    assert est_k1.source == "cluster" and est_k1.confidence == "low"
    deal = assess(candidate, est_k1, fee_model_from_settings(settings()), [])
    result = match(BuyRule("Lunar Snake", 5.0), candidate, deal)  # default min_confidence=med
    assert not result.ok and result.code == "LOW_CONFIDENCE"


def test_a_cluster_of_three_is_med_and_enough_comparables_is_high():
    candidate = L("c", 1.0)
    three = [L("a", 1.60), L("b", 1.62), L("c2", 1.65)]
    assert estimate_floor(candidate, three, None, [], NOW).confidence == "med"
    assert estimate_floor(candidate, three, None, [], NOW, min_comparables=3).confidence == "high"
    spread = [L("a", 1.60), L("b", 3.00), L("c2", 6.00)]  # three listings, but no cluster: a thin floor
    assert estimate_floor(candidate, spread, None, [], NOW).confidence == "low"
    assert estimate_floor(candidate, [], 1.5, [], NOW).confidence == "low"  # the API floor alone


# --- price units fail closed all the way to buy() ---------------------------------------------------------


def test_unknown_price_unit_never_reaches_buy_even_live():
    async def go():
        clock = Clock()
        client = MockClient("demo", now=clock())
        for raw in client._book:
            if raw["id"] == "ls-4410":
                raw["currency"] = "STARS"
        engine, storage, _, _, clock = await demo_engine(
            settings(DRY_RUN=False),
            rules=[BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="bot")],
            force_dry=False, clock=clock, client=client)
        try:
            await engine.arm_live()
            report = await engine.run_cycle()
            assert "ls-4410" not in client.bought and report.reasons["BAD_PRICE"] == 1
        finally:
            await storage.close()

    run(go())


# --- smaller parity fixes ---------------------------------------------------------------------------------


def test_best_exit_line_compares_market_codes_not_bot_usernames():
    listing = L("a", 1.0)
    deal = assess(listing, estimate_floor(listing, [L(f"p{i}", 1.6 + i / 100) for i in range(5)], None, [], NOW),
                  fee_model_from_settings(settings()), [])
    view = DealView(listing=listing, deal=deal, rule=BuyRule("Lunar Snake", 2.0), mode="auto", status="",
                    practice=True, best_exit=("mrkt", 1.5), market_bot="my_mrkt_bot", token="t")
    assert "Best exit" not in render_deal_card(view).plain


def test_peer_view_does_not_wait_for_the_ton_rate_without_telegram_data():
    calls = []

    async def fetch(url):
        calls.append(url)
        return {"rates": {"TON": {"prices": {"USD": 1.2}}}}

    async def go():
        s = settings(TG_VALUE_ENABLED=True)
        tg = TgValueService(None, ttl=60, max_per_min=10)
        peers = Peers(s, "mrkt", tg=tg, rate=TonRate("http://rate", 600, fetch=fetch))
        listing = L("a", 1.0)
        deal = assess(listing, estimate_floor(listing, [], 1.5, [], NOW), fee_model_from_settings(s), [])
        await peers.view(listing, BuyRule("Lunar Snake", 2.0), deal)
        assert calls == []

    run(go())


def test_engine_on_fresh_db_reports_practice_in_status():
    async def go():
        storage = await Storage(":memory:").open()
        try:
            engine = Engine(settings(DRY_RUN=False), storage, MockClient(), MemoryNotifier())
            state = await engine.safety_state()
            assert not state.live and not state.locked
        finally:
            await storage.close()

    run(go())
