import asyncio
import csv
import io

import pytest
from helpers import NOW, L

from mrkt_autobuy.ledger import CSV_HEADER, Stats, export_csv, format_pnl, headline, mark_positions, summarize, verdict
from mrkt_autobuy.pricing import FeeModel
from mrkt_autobuy.storage import Position, Storage
from helpers import settings


def pos(pid, cost, mark_net=None, *, dry=True, rule_id=1, created=NOW - 3600, closed=None, exit_price=None):
    return Position(pid, f"l{pid}", "Lunar Snake", "Albino", f"Lunar Snake #{pid}", cost, cost, rule_id, dry,
                    "dry" if dry else "ok", created, closed, exit_price, mark_net=mark_net)


@pytest.mark.parametrize(
    "fees,expected_net",
    [(FeeModel(10, 0, 0), 2.0 - 1.1), (FeeModel(0, 5, 0.1), 1.9 - 0.1 - 1.0), (FeeModel(0, 5, 0), 1.9 - 1.0)],
    ids=["tonnel", "portals", "mrkt"],
)
def test_mark_math_with_each_market_fees(fees, expected_net):
    async def go():
        storage = await Storage(":memory:").open()
        try:
            await storage.record_purchase(L("a", 1.0), floor_ton=None, rule_id=1, dry=True, status="dry",
                                          cost_ton=fees.cost(1.0), created_at=NOW - 600)
            await storage.add_snapshot(NOW - 60, "lunarsnake|albino||", 2.0, 5, "cluster")
            n = await mark_positions(storage, settings(), NOW, lambda c: fees)
            assert n == 1
            [p] = await storage.positions()
            assert p.mark_floor == 2.0 and p.mark_net - p.cost_ton == pytest.approx(expected_net)
        finally:
            await storage.close()

    asyncio.run(go())


def test_mark_falls_back_to_a_bounded_search():
    async def go():
        storage = await Storage(":memory:").open()
        calls = []

        async def search(collection, model):
            calls.append((collection, model))
            return [L(f"s{i}", p) for i, p in enumerate((1.5, 1.55, 1.6, 1.62))]

        try:
            for i, model in enumerate(("Albino", "Cobra", "Viper")):
                await storage.record_purchase(L(f"x{i}", 1.0, model=model), floor_ton=None, rule_id=1, dry=True,
                                              status="dry", created_at=NOW - 600)
            n = await mark_positions(storage, settings(MARK_MAX_QUERIES=2), NOW, lambda c: FeeModel(0, 5, 0), search)
            assert len(calls) == 2 and n == 2
        finally:
            await storage.close()

    asyncio.run(go())


def test_practice_and_live_never_mix():
    positions = [pos(1, 1.0, 1.5), pos(2, 2.0, 1.0, dry=False), pos(3, 1.0, 0.9)]
    practice = summarize(positions, practice=True, days=7, now=NOW, fees=FeeModel())
    live = summarize(positions, practice=False, days=7, now=NOW, fees=FeeModel())
    assert practice["total"].buys == 2 and practice["total"].unrealized == pytest.approx(0.4)
    assert live["total"].buys == 1 and live["total"].unrealized == pytest.approx(-1.0)
    text = format_pnl(practice, live, 7, html=False)
    assert text.index("PRACTICE 7d") < text.index("LIVE 7d")
    assert "Practice assumes you won every race for the listing; real results are worse." in text


def test_realized_uses_exit_net_of_fees():
    p = pos(1, 1.0, closed=NOW, exit_price=2.0)
    s = summarize([p], practice=True, days=7, now=NOW, fees=FeeModel(0, 5, 0.1))["total"]
    assert s.realized == pytest.approx(2.0 * 0.95 - 0.1 - 1.0) and s.unrealized == 0


def test_verdict_thresholds():
    s = settings(GATE_MIN_TRADES=5)
    ready = Stats()
    for _ in range(5):
        ready.add(1.0, 0.1, closed=False)
    assert verdict(ready, s) == "ready"
    few = Stats()
    few.add(1.0, 0.5, closed=False)
    assert verdict(few, s) == "not enough data"
    losing = Stats()
    for net in (0.1, -0.5, 0.1):
        losing.add(1.0, net, closed=False)
    assert verdict(losing, s) == "losing"
    assert verdict(None, s) == "not enough data"


def test_headline_format():
    s = summarize([pos(1, 1.0, 1.5), pos(2, 1.0, 0.4)], practice=True, days=7, now=NOW, fees=FeeModel())["total"]
    assert headline("PRACTICE", 7, s) == ("PRACTICE 7d: 2 buys · spent 2.0 TON · unrealized −0.1 TON after fees · "
                                          "win 1/2 · worst −0.6 TON")


def test_csv_header_and_rows():
    text = export_csv([pos(1, 1.0, 1.5), pos(2, 2.0, None, dry=False)], FeeModel())
    rows = list(csv.reader(io.StringIO(text)))
    assert rows[0] == CSV_HEADER
    assert rows[1][0] == "1" and rows[1][2] == "practice" and rows[1][-1] == "0.5000"
    assert rows[2][2] == "live" and rows[2][-1] == ""


def test_migration_keeps_old_purchases_in_ledger(tmp_path):
    import sqlite3

    from test_pricing import SCAFFOLD_SCHEMA

    path = tmp_path / "old.sqlite3"
    con = sqlite3.connect(path)
    con.executescript(SCAFFOLD_SCHEMA)
    for i in range(3):
        con.execute("INSERT INTO purchases(listing_id, collection, title, price_ton, dry, status, created_at, raw)"
                    " VALUES (?, 'Lunar Snake', 't', 1.0, 1, 'dry', ?, '{}')", (f"g{i}", NOW - 3600))
    con.commit()
    con.close()

    async def go():
        storage = await Storage(str(path)).open()
        try:
            positions = await storage.open_positions(NOW)
            assert len(positions) == 3 and all(p.cost_ton == 1.0 for p in positions)
        finally:
            await storage.close()

    asyncio.run(go())
