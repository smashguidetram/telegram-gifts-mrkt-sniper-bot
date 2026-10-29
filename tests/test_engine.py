import asyncio
from dataclasses import replace

import pytest

from mrkt_autobuy.client import MOCK_RULES, MockClient
from mrkt_autobuy.config import Settings
from mrkt_autobuy.engine import Engine, Limits, check_limits, next_backoff
from mrkt_autobuy.notifier import MemoryNotifier
from mrkt_autobuy.storage import Storage


def test_check_limits():
    limits = Limits(max_price_ton=5, daily_budget_ton=10, max_buys_per_hour=2)
    assert check_limits(4, limits, spent_24h=0, buys_last_hour=0) is None
    assert "MAX_PRICE_TON" in check_limits(6, limits, spent_24h=0, buys_last_hour=0)
    assert "daily budget" in check_limits(4, limits, spent_24h=7, buys_last_hour=0)
    assert "MAX_BUYS_PER_HOUR" in check_limits(1, limits, spent_24h=0, buys_last_hour=2)


def test_next_backoff():
    assert next_backoff(2, 2, 60) == 4
    assert next_backoff(4, 2, 60) == 8
    assert next_backoff(50, 2, 60) == 60
    assert next_backoff(2, 2, 60, retry_after=10) == 10
    assert next_backoff(2, 2, 60, retry_after=999) == 60


def _settings(**overrides):
    base = dict(_env_file=None, DRY_RUN=True, MAX_PRICE_TON=10, DAILY_BUDGET_TON=50, MAX_BUYS_PER_HOUR=3,
                POLL_INTERVAL=0.01, PAGE_SIZE=20)
    base.update(overrides)
    return Settings(**base)


async def _run_mock(settings, rules, cycles=2, force_dry=True, arm_live=False):
    storage = await Storage(":memory:").open()
    try:
        for r in rules:
            await storage.add_rule(replace(r, id=None))
        client = MockClient()
        notifier = MemoryNotifier()
        engine = Engine(settings, storage, client, notifier, force_dry=force_dry)
        if arm_live:  # /dry off: a fresh database is practice until LIVE is armed
            assert await engine.arm_live() is None
        reports = []
        for _ in range(cycles):
            reports.append(await engine.run_cycle())
            client.advance()
        purchases = await storage.recent_purchases(50)
        return reports, purchases, notifier.messages, client
    finally:
        await storage.close()


def test_mock_pipeline_alerts_dry_buys_and_dedupes():
    reports, purchases, messages, client = asyncio.run(_run_mock(_settings(), MOCK_RULES))
    first, second = reports
    assert first.dry_bought == 1 and first.new == 6
    assert any("rule #1" in r and "price 2.95 > max 2.5" in r for r in first.rejections)
    assert any("discount" in r for r in first.rejections)
    # cycle 2: old listings are deduped, only the fresh one is new
    assert second.new == 1 and second.dry_bought == 1
    assert [p.listing_id for p in reversed(purchases)] == ["mock-ls-1042", "mock-dc-901"]
    assert all(p.dry and p.status == "dry" for p in purchases)
    assert sum(m.startswith("DEAL") and "PRACTICE: would buy" in m for m in messages) == 2
    assert not any(m.startswith("DRY:") for m in messages)  # one card per practice buy, no second message
    assert client.bought == []  # nothing was really bought


def test_limits_block_buys():
    reports, purchases, messages, _ = asyncio.run(_run_mock(_settings(MAX_BUYS_PER_HOUR=1), MOCK_RULES))
    assert len(purchases) == 1
    assert reports[1].skipped == 1
    assert any(m.startswith("SKIP") and "MAX_BUYS_PER_HOUR" in m for m in messages)


def test_live_mode_calls_buy_when_unlocked():
    reports, purchases, messages, client = asyncio.run(
        _run_mock(_settings(DRY_RUN=False), MOCK_RULES, cycles=1, force_dry=False, arm_live=True)
    )
    assert client.bought == ["mock-ls-1042"]
    assert purchases[0].status == "ok" and not purchases[0].dry
    assert any(m.startswith("BOUGHT") for m in messages)


def test_dry_lock():
    async def go():
        storage = await Storage(":memory:").open()
        try:
            locked = Engine(_settings(DRY_RUN=True), storage, MockClient(), MemoryNotifier())
            assert await locked.is_dry()
            assert await locked.set_dry(False) is not None  # refused
            assert await locked.is_dry()
            unlocked = Engine(_settings(DRY_RUN=False), storage, MockClient(), MemoryNotifier())
            assert await unlocked.is_dry()  # fresh database: practice until LIVE is armed
            assert await unlocked.set_dry(False) is None
            assert not await unlocked.is_dry()
            assert await unlocked.set_dry(True) is None
            assert await unlocked.is_dry()
        finally:
            await storage.close()

    asyncio.run(go())


def test_paused_engine_does_nothing():
    async def go():
        storage = await Storage(":memory:").open()
        try:
            for r in MOCK_RULES:
                await storage.add_rule(replace(r, id=None))
            client = MockClient()
            engine = Engine(_settings(), storage, client, MemoryNotifier())
            await engine.set_paused(True)
            report = await engine.run_cycle()
            assert report.paused and client.requests == 0
        finally:
            await storage.close()

    asyncio.run(go())


@pytest.mark.parametrize("ids,expected", [("1, 2,3", [1, 2, 3]), ("", [])])
def test_admin_ids_parsing(ids, expected):
    assert _settings(ADMIN_IDS=ids).admin_ids == expected
