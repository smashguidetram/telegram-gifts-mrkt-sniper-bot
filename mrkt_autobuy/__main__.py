"""CLI: python -m mrkt_autobuy {login,run,once,mock,floors,doctor}."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from pydantic import ValidationError

from . import updater as _updater
from .client import ApiError
from .config import ConfigError, Settings, load_settings
from .notifier import Notifier
from .rules import RuleParseError, load_rules_file
from .storage import Storage

log = logging.getLogger("mrkt_autobuy")

PKG_DIR = Path(__file__).resolve().parent
FIXTURES = PKG_DIR.parent / "tests" / "fixtures"
MOCK_ADMIN_ID = 1


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    for noisy in ("httpx", "telethon", "aiogram.event", "aiosqlite"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


async def open_storage(settings: Settings, path: str | None = None) -> Storage:
    storage = await Storage(path or settings.DB_PATH, buy_fee_pct=settings.BUY_FEE_PCT).open()
    await storage.prune_seen()
    await storage.prune(time.time(), obs_days=settings.OBS_TTL_DAYS, snap_days=settings.SNAPSHOT_TTL_DAYS,
                        decision_days=settings.DECISIONS_TTL_DAYS)
    return storage


async def load_file_rules(settings: Settings, storage: Storage) -> None:
    try:
        rules = load_rules_file(settings.RULES_FILE)
    except (RuleParseError, json.JSONDecodeError, ValueError) as exc:
        raise ConfigError(f"RULES_FILE {settings.RULES_FILE} is invalid: {exc}") from exc
    if rules:
        # New file rules without a "state" start in shadow (practice only) when SHADOW_NEW_RULES is on.
        new_state = "shadow" if settings.SHADOW_NEW_RULES else "live"
        await storage.replace_rules_from_source("file", rules, new_state=new_state)
        log.info("loaded %d rule(s) from %s", len(rules), settings.RULES_FILE)


def make_notifier(settings: Settings):
    """Bot notifier when BOT_TOKEN + ADMIN_IDS are set, else stdout. Returns (notifier, bot|None)."""
    if settings.BOT_TOKEN and settings.admin_ids:
        from .bot import create_bot

        bot = create_bot(settings)
        return Notifier(bot, settings.admin_ids), bot
    return Notifier(), None


def build_peers(settings: Settings, session=None, clock=time.time):
    from .fx import TonRate
    from .peers import Peers, PortalsPublic, TelethonTgValue, TgValueService

    fetch = TelethonTgValue(session) if session is not None and settings.TG_VALUE_ENABLED else None
    tg = TgValueService(fetch, ttl=settings.TG_VALUE_TTL, max_per_min=settings.TG_VALUE_MAX_PER_MIN, clock=clock)
    rate = TonRate(settings.TON_RATE_URL, settings.TON_RATE_TTL, clock=clock)
    portals = PortalsPublic(settings.PORTALS_PUBLIC_URL, settings.PORTALS_PUBLIC_TTL, clock=clock) \
        if settings.PEER_PORTALS_PUBLIC else None
    return Peers(settings, "mrkt", tg=tg, rate=rate, portals=portals, clock=clock)


def telegram_session(settings: Settings):
    if not (settings.TG_API_ID and settings.TG_API_HASH):
        return None
    from .tg_auth import TelegramSession

    return TelegramSession(settings)


def make_engine(settings: Settings, storage: Storage, client, notifier, session=None):
    from .engine import Engine
    from .events import EventLog

    return Engine(
        settings, storage, client, notifier,
        peers=build_peers(settings, session),
        events=EventLog(settings.EVENTS_FILE, settings.EVENTS_MAX_MB),
        snapshot_path=Path(settings.DATA_DIR) / "floors_mrkt.json",
    )


# --- commands ---------------------------------------------------------------------


async def cmd_login(settings: Settings, args: argparse.Namespace) -> int:
    from .client import MrktClient
    from .tg_auth import interactive_login

    settings.require_telegram_user()
    await interactive_login(settings)
    session_file = settings.session_path.with_suffix(".session")
    if session_file.exists():
        session_file.chmod(0o600)  # /doctor checks this
    async with MrktClient(settings) as client:
        await client.authenticate(force=True)
        print("MRKT auth OK.")
        try:
            print(f"Balance: {await client.balance():.4f} TON")
        except Exception as exc:  # balance is informational only
            print(f"Balance request failed: {exc}")
    return 0


async def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    from .bot import AppContext, build_dispatcher, create_bot, set_bot_commands
    from .client import MrktClient
    from .doctor import make_tg_probe
    from .tg_auth import InitDataProvider, lookup_gift

    settings.require_bot()
    storage = await open_storage(settings)
    session = telegram_session(settings)
    client = MrktClient(settings, InitDataProvider(settings, session) if session else None)
    bot = create_bot(settings)
    try:
        await load_file_rules(settings, storage)
        await client.start()
        await client.authenticate()  # fail fast with a clear message if Telegram login is missing
        notifier = Notifier(bot, settings.admin_ids)
        engine = make_engine(settings, storage, client, notifier, session)
        await engine.start_in_practice()  # LIVE survives a restart only while its 24 h gate is still open
        gift_lookup = (lambda slug: lookup_gift(session, slug)) if session else None
        ctx = AppContext(settings, storage, client, engine, peers=engine.peers, tg_session=session,
                         gift_lookup=gift_lookup, tg_probe=make_tg_probe(settings, session))
        dp = build_dispatcher(ctx)
        await set_bot_commands(bot)

        stop = asyncio.Event()
        engine_task = asyncio.create_task(engine.run_forever(stop), name="engine")

        def _engine_done(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is not None:
                log.error("engine stopped: %s", task.exception())
                asyncio.ensure_future(dp.stop_polling())

        engine_task.add_done_callback(_engine_done)
        mode = "PRACTICE" if await engine.is_dry() else "LIVE"
        await notifier.send(f"MRKT autobuy started ({mode}). /safety for limits, /status for details.")
        log.info("running: mode=%s poll=%.1fs", mode, settings.POLL_INTERVAL)
        try:
            await dp.start_polling(bot, handle_signals=True)
        finally:
            # Graceful stop: no new buy starts, and a buy in flight (loop or tap) gets its answer and its
            # purchase row before the engine is told to stop (and cancelled if it hangs).
            grace = settings.HTTP_TIMEOUT + 5
            if not await engine.drain(grace):
                log.warning("a buy was still in flight after %.0fs; it stays recorded as 'unknown'", grace)
            stop.set()
            try:
                await asyncio.wait_for(engine_task, timeout=15)
            except (asyncio.CancelledError, TimeoutError):
                engine_task.cancel()
        if engine_task.done() and not engine_task.cancelled() and engine_task.exception():
            raise engine_task.exception()  # type: ignore[misc]
        return 0
    finally:
        await client.close()
        if session is not None:
            await session.close()
        await bot.session.close()
        await storage.close()


async def cmd_once(settings: Settings, args: argparse.Namespace) -> int:
    from .client import MrktClient

    storage = await open_storage(settings)
    notifier, bot = make_notifier(settings)
    try:
        await load_file_rules(settings, storage)
        if not await storage.list_rules(only_enabled=True):
            print("No rules configured: add rules.json or use /addrule or /hunt in the bot.")
            return 0
        async with MrktClient(settings) as client:
            engine = make_engine(settings, storage, client, notifier)
            await engine.start_in_practice()
            report = await engine.run_cycle()
            print(f"cycle done ({'PRACTICE' if await engine.is_dry() else 'LIVE'}): {report.summary()}")
            if report.reasons:
                print(f"skip reasons: {report.top_reasons(10)}")
        return 0
    finally:
        if bot is not None:
            await bot.session.close()
        await storage.close()


async def cmd_floors(settings: Settings, args: argparse.Namespace) -> int:
    from .client import MrktClient

    if args.json:
        snap = Path(settings.DATA_DIR) / "floors_mrkt.json"
        if snap.exists():
            print(snap.read_text(encoding="utf-8"))
            return 0
        from .peers import build_snapshot
        from .pricing import fee_model_from_settings

        async with MrktClient(settings) as client:
            floors = await client.collection_floors()
        data = {name: {"floor": floor, "n": 0, "models": {}} for name, floor in floors.items()}
        print(json.dumps(build_snapshot("mrkt", fee_model_from_settings(settings), data, time.time()), indent=2))
        return 0
    async with MrktClient(settings) as client:
        floors = await client.collection_floors()
    for name, floor in sorted(floors.items(), key=lambda kv: kv[0].lower()):
        print(f"{floor:>12.4f} TON  {name}")
    print(f"{len(floors)} entries")
    return 0


async def cmd_doctor(settings: Settings, args: argparse.Namespace) -> int:
    from .bot import AppContext, doctor_text
    from .client import MrktClient
    from .doctor import make_tg_probe

    storage = await open_storage(settings)
    client = MrktClient(settings)
    try:
        try:
            await client.start()
        except ConfigError as exc:
            print(f"(market client not started: {exc})")
        engine = make_engine(settings, storage, client, Notifier())
        ctx = AppContext(settings, storage, client, engine, peers=engine.peers,
                         tg_probe=make_tg_probe(settings))
        text = await doctor_text(ctx)
        print(text)
        return 1 if "✗" in text else 0
    finally:
        await client.close()
        await storage.close()


# --- mock -----------------------------------------------------------------------------


class MockClock:
    def __init__(self, start: float) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _mock_settings(settings: Settings, args: argparse.Namespace, mock_dir: Path, now: float) -> Settings:
    """Write the peer fixtures (ts rewritten to now) into the mock dir and point the settings at them."""
    from .peers import load_fixture_snapshot, write_snapshot

    peer_dir = mock_dir / "peers"
    files = []
    for name in ("floors_portals.json", "floors_tonnel.json"):
        src = FIXTURES / "peers" / name
        if src.exists():
            dst = peer_dir / name
            write_snapshot(dst, load_fixture_snapshot(src, now))
            files.append(str(dst))
    update = {
        "PEER_FLOOR_FILES": ",".join(files),
        "EVENTS_FILE": str(Path(settings.DATA_DIR) / "mock-events.jsonl"),
        "POLL_INTERVAL": max(settings.POLL_INTERVAL, 1.0),
    }
    if args.shadow_hours is not None:
        update.update(SHADOW_MIN_HOURS=args.shadow_hours, SHADOW_MAX_HOURS=args.shadow_hours)
    return settings.model_copy(update=update)


def _tg_fixture(now: float) -> dict:
    path = FIXTURES / "tg_value.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for slug, info in data.items():
        if slug.startswith("_"):
            continue
        info = dict(info)
        days = info.pop("last_sale_days_ago", None)
        if days is not None:
            info["last_sale_date"] = now - days * 86400
        out[slug] = info
    return out


async def _seed(storage: Storage, now: float) -> None:
    """Seed 8 practice buys over 6 days with drifting floors (5 up, 3 down) and a 24 h observation tape."""
    from .models import Listing

    def gift(lid: str, model: str, number: int, price: float) -> Listing:
        return Listing(lid, "Lunar Snake", model, "Onyx Black", "Star", number, price, raw={"seed": True})

    albino = "lunarsnake|albino||"
    cobra = "lunarsnake|cobra||"
    for i, hours_ago in enumerate((144, 120, 96, 72, 48, 24, 12, 2)):
        ts = now - hours_ago * 3600
        await storage.add_snapshot(ts, albino, round(1.30 + 0.25 * i / 7, 3), 4, "cluster")
        await storage.add_snapshot(ts, cobra, round(1.30 - 0.30 * i / 7, 3), 4, "cluster")
    seeds = [(1, "Albino", 1.10, 140), (1, "Albino", 1.15, 118), (1, "Albino", 1.20, 95), (1, "Albino", 1.25, 70),
             (1, "Albino", 1.30, 47), (2, "Cobra", 1.20, 130), (2, "Cobra", 1.35, 100), (2, "Cobra", 1.55, 60)]
    for i, (rule_id, model, price, hours_ago) in enumerate(seeds):
        listing = gift(f"seed-{i}", model, 4300 + i, price)
        await storage.record_purchase(listing, floor_ton=None, rule_id=rule_id, dry=True, status="dry",
                                      detail="seeded practice", cost_ton=price, created_at=now - hours_ago * 3600)
    tape = [("tape-1", "Albino", 1.10, 20), ("tape-2", "Albino", 1.65, 18), ("tape-3", "Cobra", 1.00, 16),
            ("tape-4", "Albino", 1.20, 11), ("tape-5", "Albino", 1.62, 9), ("tape-6", "Cobra", 0.98, 8),
            ("tape-7", "Albino", 1.05, 6), ("tape-8", "Albino", 0.90, 4)]
    for lid, model, price, hours_ago in tape:
        await storage.upsert_observations([gift(lid, model, 5000 + int(hours_ago), price)], now - hours_ago * 3600)
    await storage.add_decision(now - 4 * 3600, "tape-8", 0.90, None, "skip", "LOCKED",
                               {"text": "locked: premarket: unlocks in 5 d", "title": "Lunar Snake #5004"})


async def _fake_tap(engine, action: str, now: float, tokens: list[str] | None = None) -> None:
    """FakeTapper: run the real tap handler on every pending card, then try one double tap."""
    if tokens is None:
        tokens = [c.token for c in await engine.storage.list_pending(("pending",))]
    first = None
    for token in tokens:
        card = await engine.storage.get_pending(token)
        result = await engine.tap(action[0], token, MOCK_ADMIN_ID, now)
        if card is not None and not result.ok and "Stopped" in result.text:
            print(f"[tap {action}] {card.payload.get('title')}: {result.text}", flush=True)
            continue
        print(f"[tap {action}] {result.text}", flush=True)
        first = first or card.token
    if first and action == "buy":
        again = await engine.tap("b", first, MOCK_ADMIN_ID, now)
        print(f"[tap {action}] double tap on the same card: {again.text}", flush=True)


def _section(title: str) -> None:
    print(f"\n--- {title} ---", flush=True)


async def cmd_mock(settings: Settings, args: argparse.Namespace) -> int:
    from collections import Counter

    from .bot import (AppContext, compare_text, doctor_text, dry_off_gate, peers_text, pnl_text, safety_for,
                      score_text, why_for)
    from .client import DEMO_RULES, MockClient
    from .digest import build_digest
    from .engine import DAY, Engine
    from .events import EventLog, read_events, validate_event
    from .fx import TonRate
    from .ledger import scoreboard
    from .peers import FakeTgValue, Peers, TgValueService, read_snapshot
    from .rules import parse_addrule_args
    from .wizard import preview_for

    start = time.time()
    clock = MockClock(start)
    mock_dir = Path(settings.DATA_DIR) / "mock"
    s = _mock_settings(settings, args, mock_dir, start)
    events_path = Path(s.EVENTS_FILE)
    events_path.parent.mkdir(parents=True, exist_ok=True)
    events_path.write_text("", encoding="utf-8")

    storage = await open_storage(s, ":memory:")  # fresh state on every mock run
    # Always stdout, even with BOT_TOKEN and ADMIN_IDS in .env: the mock never talks to Telegram.
    notifier = Notifier()
    client = MockClient("demo", now=start)
    tg = TgValueService(FakeTgValue(_tg_fixture(start)), ttl=s.TG_VALUE_TTL, max_per_min=s.TG_VALUE_MAX_PER_MIN,
                        clock=clock)
    peers = Peers(s, "mrkt", tg=tg, rate=TonRate(s.TON_RATE_URL, fixed=1.25), clock=clock)
    try:
        for rule in DEMO_RULES:
            await storage.add_rule(replace(rule, id=None))
        if args.shadow_hours is not None:
            await storage.update_rule(1, state="shadow")
            await storage.set_setting("shadow_since:1", start - 6 * DAY)
        await _seed(storage, start)
        engine = Engine(s, storage, client, notifier, force_dry=True, clock=clock, peers=peers,
                        events=EventLog(events_path, s.EVENTS_MAX_MB), snapshot_path=mock_dir / "floors_mrkt.json")
        engine.admin_ids.add(MOCK_ADMIN_ID)
        ctx = AppContext(s, storage, client, engine, peers=peers)
        rules = await storage.list_rules()
        print(f"MOCK mode: {len(rules)} demo rules, {args.cycles} cycle(s), PRACTICE forced (DRY_RUN lock), "
              f"tap={args.tap}")
        for rule in rules:
            print(f"  rule {rule.describe()}")
        print(f"  {engine.fees.describe(s.FEES_VERIFIED)}")

        totals: Counter = Counter()
        doctor_report = ""
        for i in range(1, args.cycles + 1):
            _section(f"cycle {i}")
            before = client.requests
            report = await engine.run_cycle()
            totals.update(report.reasons)
            print(f"cycle {i}: {report.summary()} · requests={client.requests - before}")
            for line in report.rejections:
                print(f"  {line}")
            tokens = None
            if args.panic and i == 1:
                _section("/panic")
                tokens = [c.token for c in await storage.list_pending(("pending",))]
                await engine.panic()
                print(await safety_for(ctx))
            if args.tap != "none":
                if i == 1 and not args.panic:
                    client.reprice("dc-3002", 2.40)  # someone repriced the confirm card's listing before the tap
                await _fake_tap(engine, args.tap, clock(), tokens)
            if i == args.cycles and args.doctor:
                doctor_report = await doctor_text(ctx)
            client.advance()
            clock.advance(120)
            await engine.tick(marks_search=False)

        if args.crash:
            _section("crash cycle")
            client.crash("Lunar Snake", "Cobra", 0.7)
            report = await engine.run_cycle()
            totals.update(report.reasons)
            print(f"crash cycle: {report.summary()}")
            clock.advance(120)

        now = clock()
        await engine.run_marks(now, search=False)
        _section("/pnl")
        print(await pnl_text(ctx, 7, html_mode=False))
        _section("/score")
        print(await score_text(ctx))
        _section("/dry off (gate, as it would answer with DRY_RUN=false)")
        gate_text, gate_kb = await dry_off_gate(ctx, False, preview=True)
        print(gate_text.split("\n\n")[-1])
        print(f"[buttons: {' | '.join(b.text for row in gate_kb.inline_keyboard for b in row)}]")
        print((await dry_off_gate(ctx, False))[0].split("\n")[0])
        _section("/scoreboard")
        print(await scoreboard(storage, s, await storage.list_rules(only_enabled=True), now, engine.fees))
        _section("/why ls-4412 (the listing that loses money after fees)")
        print(await why_for(ctx, "ls-4412"))
        _section("digest preview")
        print(await build_digest(engine, now - DAY, now + 1, title="Digest, last 24 h"))
        _section("/safety")
        print(await safety_for(ctx))
        _section("/peers")
        print(await peers_text(ctx))
        _section("/compare Spy Agaric")
        print(await compare_text(ctx, "Spy Agaric", None))

        _section("files")
        snap = read_snapshot(str(mock_dir / "floors_mrkt.json"), "other", now, 10**9)
        print(f"own snapshot {mock_dir / 'floors_mrkt.json'}: {snap.status}, "
              f"{len((snap.data or {}).get('collections', {}))} collections")
        events = read_events(events_path)
        bad = sum(1 for e in events if validate_event(e))
        print(f"events {events_path}: {len(events)} lines, {bad} invalid")

        if args.preview:
            _section(f"preview: {args.preview}")
            print(f"Rule: {parse_addrule_args(args.preview).describe()}")
            print((await preview_for(ctx, parse_addrule_args(args.preview), now)).text)
        if args.doctor:
            _section("doctor (mock, right after the last cycle)")
            print(doctor_report or await doctor_text(ctx))

        _section("summary")
        purchases = await storage.recent_purchases(100)
        fresh = [p for p in purchases if p.detail != "seeded practice"]
        print(f"recorded purchases: {len(purchases)} ({len(purchases) - len(fresh)} seeded)")
        for p in reversed(fresh):
            print(f"  #{p.id} {'PRACTICE' if p.dry else p.status} {p.title} {p.cost_ton:g} TON all-in "
                  f"(floor {p.floor_ton:.2f})" if p.floor_ton else f"  #{p.id} {p.title}")
        print(f"client.buy() calls: {len(client.bought)} (practice never buys)")
        print("skip reasons: " + " · ".join(f"{k} {v}" for k, v in totals.most_common()))
        return 0
    finally:
        await storage.close()


COMMANDS = {
    "login": cmd_login,
    "run": cmd_run,
    "once": cmd_once,
    "mock": cmd_mock,
    "floors": cmd_floors,
    "doctor": cmd_doctor,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mrkt_autobuy",
        description="MRKT (Telegram gifts marketplace) autobuy / sniper with net-of-fees deal math. "
        "Practice mode (DRY_RUN=true) by default.",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    sub.add_parser("login", help="interactive Telegram login (Telethon) + MRKT auth check")
    sub.add_parser("run", help="sniping loop + admin bot")
    sub.add_parser("once", help="run a single scan/evaluate cycle and exit")
    p_mock = sub.add_parser("mock", help="offline pipeline with fixture data (no credentials, no Telegram)")
    p_mock.add_argument("--cycles", type=int, default=2, help="number of cycles (default 2)")
    p_mock.add_argument("--tap", choices=("none", "buy", "skip"), default="none",
                        help="simulate tapping this button on every card that waits for a tap, after each cycle")
    p_mock.add_argument("--crash", action="store_true", help="add a cycle where a floor falls 30%%")
    p_mock.add_argument("--panic", action="store_true", help="press /panic after cycle 1")
    p_mock.add_argument("--preview", metavar="RULE", help='preview a rule on the 24 h tape, e.g. "Lunar Snake profit=0.3 max=3"')
    p_mock.add_argument("--doctor", action="store_true", help="print /doctor against the mock client")
    p_mock.add_argument("--shadow-hours", type=float, default=None, metavar="H",
                        help="start rule #1 in shadow and print its hindsight card after H hours (0 = now)")
    p_floors = sub.add_parser("floors", help="print collection floors from the API")
    p_floors.add_argument("--json", action="store_true", help="print this bot's floor snapshot as JSON (the file other bots read)")
    sub.add_parser("doctor", help="self-check: config, credentials, API, DB, clock; exit 1 on any failure")
    return parser


def main(argv: list[str] | None = None) -> int:
    _updater.startup()
    if sys.platform != "win32" and os.environ.get("ALLOW_UNSUPPORTED_OS") != "1":
        print("This build is Windows-only. Install it on Windows 10/11, or set ALLOW_UNSUPPORTED_OS=1 to run it anyway (unsupported).", file=sys.stderr)
        return 1
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings()
    except ValidationError as exc:
        keys = ", ".join(str(err["loc"][0]) for err in exc.errors() if err.get("loc"))
        print(f"Config error: invalid value for {keys} in .env", file=sys.stderr)
        return 2
    setup_logging(settings.LOG_LEVEL)
    try:
        return asyncio.run(COMMANDS[args.command](settings, args))
    except ConfigError as exc:
        print(f"Config error: {exc}", file=sys.stderr)
        return 2
    except ApiError as exc:
        print(f"MRKT API error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
