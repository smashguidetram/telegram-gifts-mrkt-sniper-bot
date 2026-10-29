"""End-to-end acceptance checks on the F1-F8 demo fixtures (offline)."""

import asyncio

from helpers import demo_engine, settings

from mrkt_autobuy.fx import TonRate
from mrkt_autobuy.peers import FakeTgValue, Peers, TgValueService, build_snapshot, write_snapshot
from mrkt_autobuy.pricing import FeeModel


async def _live_two_cycles(tmp_path=None, **kw):
    s = settings(DRY_RUN=False, MAX_BUYS_PER_HOUR=20, **kw)
    engine, storage, client, notifier, clock = await demo_engine(s, force_dry=False)
    await engine.arm_live()
    await engine.run_cycle()
    client.advance()
    clock.advance(120)
    await engine.run_cycle()
    return engine, storage, client, notifier


def test_f1_floor_is_cluster_with_confidence_label():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine()
        try:
            await engine.run_cycle()
            card = next(m for m in notifier.messages if m.startswith("DEAL Lunar Snake #4410"))
            assert "floor 1.60 (cluster, 4 comparables, high)" in card and "3.00" not in card.split("\n")[0]
            assert "Floor 1.60 (high, 4 comparables)" in card
        finally:
            await storage.close()

    asyncio.run(go())


def test_traps_locks_and_losers_never_reach_buy_even_live():
    async def go():
        engine, storage, client, notifier = await _live_two_cycles()
        try:
            for lid in ("ls-4412", "jb-53", "jb-54", "ls-4416"):  # F2, F3, F5, F4
                assert lid not in client.bought
            assert "ls-4410" in client.bought and "ls-4417" in client.bought  # F1, F6 (auto, healthy)
            f2 = await storage.decisions_for("ls-4412")
            assert f2[0].reason_code == "LOSES_AFTER_FEES"
            assert not any("#4412" in m for m in notifier.messages)  # never alerted
            f3 = await storage.decisions_for("jb-53")
            assert f3[0].reason_code == "LOCKED" and "6 d" in f3[0].detail["text"]
            assert (await storage.decisions_for("jb-54"))[0].reason_code == "TRAP_LUCKY"
            f4 = next(m for m in notifier.messages if m.startswith("DEAL Lunar Snake #4416"))
            assert "Asking first: price is 10% of floor" in f4 and "Waiting for your tap" in f4
        finally:
            await storage.close()

    asyncio.run(go())


def test_f7_f8_downgrade_with_second_opinion(tmp_path):
    async def go():
        portals = tmp_path / "floors_portals.json"
        engine, storage, client, notifier, clock = await demo_engine(settings(PEER_FLOOR_FILES=str(portals)))
        write_snapshot(portals, build_snapshot("portals", FeeModel(0, 5, 0.1),
                                               {"Spy Agaric": {"floor": 4.6}, "Lunar Snake": {"floor": 1.7}}, clock()))
        tg = TgValueService(FakeTgValue({"HomemadeCake-1234": {"currency": "USD", "average_price": 300,
                                                                "last_sale_price": 325, "listed_count": 9}}), clock=clock)
        engine.peers = Peers(engine.settings, "mrkt", tg=tg, rate=TonRate("x", fixed=1.25), clock=clock)
        try:
            await engine.run_cycle()
            f7 = next(m for m in notifier.messages if m.startswith("DEAL Spy Agaric #777"))
            assert "Asking first: cheaper on Portals (4.60 all-in)" in f7
            f8 = next(m for m in notifier.messages if m.startswith("DEAL Homemade Cake #1234"))
            assert "Telegram: avg ~2.40 TON · last sale ~2.60 TON" in f8
            assert "Asking first: above Telegram's average (2.40 TON) and last sale (2.60 TON)" in f8
            assert "Elsewhere: Portals 4.60 (all-in)" in f7
        finally:
            await storage.close()

    asyncio.run(go())


def test_status_fee_line_unverified():
    from mrkt_autobuy.bot import AppContext, status_text

    async def go():
        engine, storage, client, notifier, clock = await demo_engine()
        try:
            await engine.run_cycle()
            text = await status_text(AppContext(engine.settings, storage, client, engine))
            assert "Fees: buy 0% · sell 5% · withdraw 0 TON (unverified)" in text
            assert "Top skip reasons (24 h): OVER_MAX" in text
            assert text.startswith("Mode: PRACTICE")
        finally:
            await storage.close()

    asyncio.run(go())
