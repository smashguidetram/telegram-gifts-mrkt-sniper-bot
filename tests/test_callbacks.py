import asyncio
from dataclasses import replace

from helpers import ADMIN, demo_engine, settings

from mrkt_autobuy.bot import AppContext, why_for
from mrkt_autobuy.client import DEMO_RULES
from mrkt_autobuy.rules import BuyRule

CONFIRM_RULES = [replace(r, mode="confirm") if r.mode == "auto" else r for r in DEMO_RULES]


async def _confirm_card(*, arm: bool = False, **kw):
    """Run one demo cycle (every rule asks first) and return the engine plus the Desk Calendar card token.
    arm=True arms LIVE before the cycle, so the cards are rendered (and labelled) LIVE."""
    kw.setdefault("rules", CONFIRM_RULES)
    engine, storage, client, notifier, clock = await demo_engine(**kw)
    if arm:
        assert await engine.arm_live() is None
    await engine.run_cycle()
    cards = await storage.list_pending(("pending",))
    card = next(c for c in cards if c.listing_id == "dc-3002")
    return engine, storage, client, notifier, clock, card.token


def test_confirm_rule_never_buys_without_a_tap():
    async def go():
        engine, storage, client, notifier, clock, token = await _confirm_card()
        try:
            purchases = await storage.recent_purchases(50)
            assert "dc-3002" not in [p.listing_id for p in purchases]
            card = await storage.get_pending(token)
            assert card.status == "pending" and card.payload["mode"] == "confirm"
        finally:
            await storage.close()

    asyncio.run(go())


def test_two_concurrent_buy_taps_buy_once():
    async def go():
        s = settings(DRY_RUN=False)
        engine, storage, client, notifier, clock, token = await _confirm_card(s=s, force_dry=False, arm=True)
        try:
            results = await asyncio.gather(engine.tap("b", token, ADMIN), engine.tap("b", token, ADMIN))
            assert client.bought.count("dc-3002") == 1
            assert sorted(r.ok for r in results) == [False, True]
            assert any("Expired or already handled" in r.text for r in results)
            assert (await storage.get_pending(token)).status == "bought"
        finally:
            await storage.close()

    asyncio.run(go())


def test_expired_token_does_not_buy():
    async def go():
        engine, storage, client, notifier, clock, token = await _confirm_card(s=settings(DRY_RUN=False), force_dry=False,
                                                                              arm=True)
        try:
            clock.advance(91)
            await engine.tick(marks_search=False)
            assert (await storage.get_pending(token)).status == "expired"
            assert any(e["note"].endswith("Expired") for e in notifier.edits)
            res = await engine.tap("b", token, ADMIN)
            assert not res.ok and res.text == "Expired or already handled"
            assert client.bought == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_changed_price_or_vanished_listing_does_not_buy():
    async def go():
        engine, storage, client, notifier, clock, token = await _confirm_card(s=settings(DRY_RUN=False), force_dry=False,
                                                                              arm=True)
        try:
            client.reprice("dc-3002", 2.40)
            res = await engine.tap("b", token, ADMIN)
            assert not res.ok and "price changed 2.20 → 2.40" in res.text
            assert (await storage.get_pending(token)).status == "changed"
            # a second card, then the listing disappears
            card = next(c for c in await storage.list_pending(("pending",)) if c.listing_id == "sa-777")
            client.remove("sa-777")
            gone = await engine.tap("b", card.token, ADMIN)
            assert not gone.ok and gone.status == "gone"
            # network error on the re-check: no buy, card stays tappable
            other = next(c for c in await storage.list_pending(("pending",)) if c.listing_id == "hc-1234")
            client.fail_lookups = True
            err = await engine.tap("b", other.token, ADMIN)
            assert not err.ok and "couldn't re-check" in err.text
            assert client.bought == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_non_admin_is_refused():
    async def go():
        engine, storage, client, notifier, clock, token = await _confirm_card()
        try:
            for action in ("b", "s", "m", "w"):
                res = await engine.tap(action, token, user_id=999)
                assert res.text == "Not allowed"
            assert (await storage.get_pending(token)).status == "pending"
            assert (await engine.near_action("r", token, 999)).text == "Not allowed"
        finally:
            await storage.close()

    asyncio.run(go())


def test_practice_tap_records_dry_purchase_once():
    async def go():
        engine, storage, client, notifier, clock, token = await _confirm_card()
        try:
            res = await engine.tap("b", token, ADMIN)
            assert res.ok and res.text == "re-check ok → PRACTICE: bought Desk Calendar #3002 for 2.20 TON"
            again = await engine.tap("b", token, ADMIN)
            assert not again.ok
            dry = [p for p in await storage.recent_purchases(50) if p.listing_id == "dc-3002"]
            assert len(dry) == 1 and dry[0].dry and client.bought == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_mute_suppresses_cards_but_decisions_are_still_written():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(MAX_BUYS_PER_HOUR=10))
        try:
            await storage.add_mute("deskcalendar", clock() + 3600)
            await engine.run_cycle()
            assert not any("Desk Calendar #3002" in m for m in notifier.messages)
            decisions = await storage.decisions_for("dc-3002")
            assert decisions and decisions[0].outcome == "muted" and decisions[0].reason_code == "MUTED"
            # auto-mode buys still notify even for a muted key
            await storage.add_mute("lunarsnake:albino", clock() + 3600)
            client.advance()
            await engine.run_cycle()
            assert any(m.startswith("DEAL Lunar Snake #4417") and "PRACTICE: would buy" in m for m in notifier.messages)
        finally:
            await storage.close()

    asyncio.run(go())


def test_mute_button_mutes_and_skips():
    async def go():
        engine, storage, client, notifier, clock, token = await _confirm_card()
        try:
            res = await engine.tap("m", token, ADMIN)
            assert res.ok and "deskcalendar:classic" in res.text
            assert await storage.muted_key(["deskcalendar:classic"], clock()) == "deskcalendar:classic"
            assert (await storage.get_pending(token)).status == "skipped"
        finally:
            await storage.close()

    asyncio.run(go())


def test_near_miss_raise_never_exceeds_max_price():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(MAX_PRICE_TON=2.6))
        try:
            await engine.run_cycle()
            near = await storage.list_pending(("near",))
            assert len(near) == 1 and near[0].payload["new_max"] == 2.65
            assert any(s["silent"] for s in notifier.sent if s["plain"].startswith("NEAR MISS"))
            res = await engine.near_action("r", near[0].token, ADMIN)
            assert not res.ok and "MAX_PRICE_TON 2.6" in res.text
            assert (await storage.get_rule(3)).max_price_ton == 2.5
        finally:
            await storage.close()

        engine, storage, client, notifier, clock = await demo_engine()
        try:
            await engine.run_cycle()
            near = (await storage.list_pending(("near",)))[0]
            res = await engine.near_action("r", near.token, ADMIN)
            assert res.ok and (await storage.get_rule(3)).max_price_ton == 2.65
        finally:
            await storage.close()

    asyncio.run(go())


def test_near_miss_capped_per_hour():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(NEAR_MISS_MAX_PER_HOUR=0))
        try:
            await engine.run_cycle()
            assert await storage.list_pending(("near",)) == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_why_for_stored_decision():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine()
        try:
            await engine.run_cycle()
            ctx = AppContext(engine.settings, storage, client, engine)
            text = await why_for(ctx, "ls-4412")
            assert text.startswith("Why: Lunar Snake #4412")
            assert "Skipped" in text and "loses 0.02 TON after fees" in text and "floor 1.00 (high, 3 listings)" in text
            link = await why_for(ctx, "https://t.me/nft/LunarSnake-4412")
            assert "loses 0.02 TON after fees" in link
            card = next(c for c in await storage.list_pending(("pending",)) if c.listing_id == "dc-3002")
            tap = await engine.tap("w", card.token, ADMIN)
            assert tap.reply and "Alerted" in tap.reply
        finally:
            await storage.close()

    asyncio.run(go())


def test_auto_rule_without_downgrade_keeps_scaffold_live_behaviour():
    async def go():
        rules = [BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, source="demo")]
        engine, storage, client, notifier, clock = await demo_engine(settings(DRY_RUN=False), rules=rules,
                                                                     force_dry=False)
        try:
            await engine.arm_live()
            await engine.run_cycle()
            assert client.bought == ["ls-4410"]
            assert any(m.startswith("BOUGHT") for m in notifier.messages)
        finally:
            await storage.close()

    asyncio.run(go())
