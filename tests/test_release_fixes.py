"""Regression tests for the v0.1.0 release check: the .env lock cancels an armed live window, one-time
resume buttons, /start onboarding, near-miss expiry, finite limits, /unmute, an offline mock, and the
wording of /help and /doctor."""

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from helpers import ADMIN, Clock, demo_engine, settings
from pydantic import ValidationError

from aiogram.filters import CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from mrkt_autobuy import __main__ as cli
from mrkt_autobuy.bot import HELP, INTRO, AppContext, build_router
from mrkt_autobuy.callbacks import GateCB, ResumeCB
from mrkt_autobuy.client import DEMO_RULES, MockClient
from mrkt_autobuy.doctor import DoctorDeps, check_bot, check_mode
from mrkt_autobuy.engine import Engine
from mrkt_autobuy.notifier import MemoryNotifier
from mrkt_autobuy.storage import Storage

DAY = 86400.0


class Msg:
    """Just enough of aiogram's Message for the admin handlers."""

    def __init__(self):
        self.out: list[tuple[str, list]] = []
        self.chat = SimpleNamespace(id=ADMIN)
        self.from_user = SimpleNamespace(id=ADMIN)
        self.reply_to_message = None
        self.message_id = 1

    async def answer(self, text, **kw):
        kb = kw.get("reply_markup")
        self.out.append((text, [b for row in kb.inline_keyboard for b in row] if kb else []))


class CB:
    def __init__(self):
        self.from_user = SimpleNamespace(id=ADMIN)
        self.message = None
        self.answers: list[str] = []

    async def answer(self, text="", **kw):
        self.answers.append(text)


def handlers():
    router = build_router([ADMIN])
    return ({h.callback.__name__: h.callback for h in router.message.handlers},
            {h.callback.__name__: h.callback for h in router.callback_query.handlers})


def cmd(name: str, args: str | None = None) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args)


# --- high: DRY_RUN=true cancels an armed live window ------------------------------------------------------


def test_dry_run_true_cancels_an_armed_live_window_for_good(tmp_path):
    """Arm LIVE, restart with DRY_RUN=true, then restart with DRY_RUN=false inside the old 24 h window:
    still practice, and the loop never calls client.buy() until a new /dry off."""

    async def engine_on(db, clock, **env):
        storage = await Storage(str(db)).open()
        client = MockClient("demo", now=clock())
        return Engine(settings(**env), storage, client, MemoryNotifier(), clock=clock), storage, client

    async def go():
        db, clock = tmp_path / "t.sqlite3", Clock()
        e1, st, _ = await engine_on(db, clock, DRY_RUN=False)
        for r in DEMO_RULES:
            await st.add_rule(replace(r, id=None))
        assert await e1.arm_live() is None and not await e1.is_dry()
        await st.close()

        clock.advance(600)
        e2, st, _ = await engine_on(db, clock, DRY_RUN=True)
        await e2.start_in_practice()
        assert await e2.is_dry() and await st.get_setting("live_until") is None
        await st.close()

        clock.advance(2 * 3600)
        e3, st, c3 = await engine_on(db, clock, DRY_RUN=False)
        try:
            await e3.start_in_practice()
            assert await e3.is_dry()
            report = await e3.run_cycle()
            assert report.bought == 0 and c3.bought == []
            assert await e3.arm_live() is None and not await e3.is_dry()  # a new /dry off still works
        finally:
            await st.close()

    asyncio.run(go())


def test_the_env_lock_voids_a_window_even_without_a_restart_hook():
    """is_dry() itself cancels a stored window under the lock (doctor, once, a directly built Engine)."""

    async def go():
        engine, storage, *_ = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            assert await engine.arm_live() is None
            locked = Engine(settings(DRY_RUN=True), storage, MockClient(), MemoryNotifier(), clock=engine.clock)
            assert await locked.is_dry()
            assert await engine.live_until() is None and await engine.is_dry()
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: one-time resume buttons ----------------------------------------------------------------------


def test_an_old_resume_button_never_undoes_a_newer_panic():
    async def go():
        engine, storage, client, *_ = await demo_engine()
        ctx = AppContext(engine.settings, storage, client, engine)
        m, c = handlers()
        try:
            await m["cmd_panic"](Msg(), ctx)
            msg = Msg()
            await m["cmd_resume"](msg, ctx)
            [confirm, keep] = msg.out[0][1]
            assert confirm.text == "Confirm resume" and confirm.callback_data.startswith("rs:")
            old = ResumeCB.unpack(confirm.callback_data)
            cb = CB()
            await c["on_resume"](cb, old, ctx)
            assert cb.answers[0].startswith("Resumed") and not await engine.is_panic()

            cb = CB()
            await c["on_resume"](cb, old, ctx)  # one-time
            assert "out of date" in cb.answers[0]

            msg = Msg()
            await m["cmd_resume"](msg, ctx)
            pending = ResumeCB.unpack(msg.out[0][1][0].callback_data)
            await m["cmd_panic"](Msg(), ctx)  # a newer /panic voids the earlier prompt
            for data in (pending, GateCB(action="resume")):  # also the pre-nonce 'g:resume' callback
                cb = CB()
                await c["on_resume" if isinstance(data, ResumeCB) else "on_gate"](cb, data, ctx)
                assert "out of date" in cb.answers[0]
                assert await engine.is_panic() and await engine.is_paused()
        finally:
            await storage.close()

    asyncio.run(go())


def test_balance_breaker_resume_button_is_voided_by_a_later_stop():
    async def go():
        engine, storage, *_ = await demo_engine()
        try:
            nonce = await engine.issue_resume_nonce()  # what the balance breaker's [Resume] carries
            await engine.set_paused(True)  # /pause (or /panic, or another breaker) afterwards
            assert not await engine.resume_with(nonce) and await engine.is_paused()
            fresh = await engine.issue_resume_nonce()
            engine.clock.advance(601)  # and every resume button expires after 10 min
            assert not await engine.resume_with(fresh) and await engine.is_paused()
            fresh = await engine.issue_resume_nonce()
            assert await engine.resume_with(fresh) and not await engine.is_paused()
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: /start onboarding ----------------------------------------------------------------------------


def test_start_says_what_the_bot_does_its_mode_and_three_commands_to_try():
    async def go():
        engine, storage, client, *_ = await demo_engine(rules=[])
        ctx = AppContext(engine.settings, storage, client, engine)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=ADMIN, user_id=ADMIN))
        m, _ = handlers()
        try:
            msg = Msg()
            await m["cmd_start"](msg, ctx, state)
            text = msg.out[0][0]
            assert text.startswith(INTRO) and "watches the MRKT marketplace" in text
            assert "Mode: PRACTICE" in text
            tries = text.split("Try these first:")[1]
            assert [w for w in tries.split() if w.startswith("/")][:3] == ["/hunt", "/scoreboard", "/doctor"]
            assert "MRKT Autobuy commands" not in text  # the full list is /help
            assert len(msg.out) == 2  # no rules yet: the /hunt wizard opens right after

            msg = Msg()
            await m["cmd_help"](msg, ctx)
            assert HELP in msg.out[0][0]
        finally:
            await storage.close()

    asyncio.run(go())


# --- medium: the mock never talks to Telegram ---------------------------------------------------------------


def test_mock_prints_cards_offline_even_with_bot_keys_in_env(tmp_path, monkeypatch, capsys):
    def no_telegram(*a, **kw):
        raise AssertionError("mock must not create a Telegram bot")

    monkeypatch.setattr("mrkt_autobuy.bot.create_bot", no_telegram)
    monkeypatch.setattr(cli, "make_notifier", no_telegram)
    s = settings(BOT_TOKEN="123456789:" + "A" * 35, ADMIN_IDS="1", DATA_DIR=str(tmp_path))
    args = cli.build_parser().parse_args(["mock", "--cycles", "1"])
    assert asyncio.run(cli.cmd_mock(s, args)) == 0
    out = capsys.readouterr().out
    assert "[notify] DEAL Lunar Snake #4410" in out and "client.buy() calls: 0" in out


# --- low: near-miss buttons expire and only ever raise ---------------------------------------------------------


def test_near_miss_raise_expires_after_24h_and_never_lowers_a_max():
    async def go():
        engine, storage, *_ = await demo_engine()
        try:
            await engine.run_cycle()
            [near] = await storage.list_pending(("near",))
            assert near.rule_id == 3 and near.payload["new_max"] == 2.65
            await storage.update_rule(3, max_price_ton=4.0)  # raised by hand since the card was sent
            res = await engine.near_action("r", near.token, ADMIN)
            assert "already 4.00" in res.text and (await storage.get_rule(3)).max_price_ton == 4.0
        finally:
            await storage.close()

        engine, storage, _, _, clock = await demo_engine()
        try:
            await engine.run_cycle()
            [near] = await storage.list_pending(("near",))
            clock.advance(DAY + 1)
            res = await engine.near_action("r", near.token, ADMIN)
            assert not res.ok and res.text == "Expired or already handled"
            assert (await storage.get_rule(3)).max_price_ton == 2.5
            assert (await engine.near_action("i", near.token, ADMIN)).text == "Expired or already handled"
        finally:
            await storage.close()

    asyncio.run(go())


# --- low: limits must be finite ---------------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["MAX_PRICE_TON", "DAILY_BUDGET_TON", "LIVE_ARM_HOURS", "BALANCE_TOLERANCE_TON"])
@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_non_finite_numbers_in_env_are_refused(key, value):
    with pytest.raises(ValidationError):
        settings(**{key: value})


def test_cli_names_the_non_finite_key(monkeypatch, capsys):
    monkeypatch.setenv("MAX_PRICE_TON", "nan")
    assert cli.main(["mock", "--cycles", "1"]) == 2
    assert "invalid value for MAX_PRICE_TON" in capsys.readouterr().err


# --- low: /unmute, wording ------------------------------------------------------------------------------------


def test_unmute_accepts_what_mute_accepts():
    async def go():
        engine, storage, client, *_ = await demo_engine()
        ctx = AppContext(engine.settings, storage, client, engine)
        m, _ = handlers()
        try:
            for target, key in (("Lunar Snake", "lunarsnake"), ("Lunar Snake:Albino", "lunarsnake:albino")):
                msg = Msg()
                await m["cmd_mute"](msg, cmd("mute", target), ctx)
                assert msg.out[0][0].startswith(f"Muted {key} ")
                msg = Msg()
                await m["cmd_unmute"](msg, cmd("unmute", target), ctx)
                assert msg.out[0][0] == f"Unmuted {key}."
            assert await storage.list_mutes(engine.clock()) == []
        finally:
            await storage.close()

    asyncio.run(go())


def test_help_and_doctor_describe_dry_off_truthfully():
    assert "/dry off confirm" in HELP and "arms LIVE" not in HELP

    async def go():
        engine, storage, *_ = await demo_engine(settings(DRY_RUN=True), force_dry=False)
        unlocked, *_ = await demo_engine(settings(DRY_RUN=False), force_dry=False)
        try:
            mode = await check_mode(DoctorDeps(engine.settings, engine, now=engine.clock()))
            assert mode.status == "ok" and "DRY_RUN=false" not in mode.line()  # the safe default is not a problem
            mode = await check_mode(DoctorDeps(unlocked.settings, unlocked, now=unlocked.clock()))
            assert mode.status == "ok" and "arms LIVE" not in mode.text
        finally:
            await storage.close()
            await unlocked.storage.close()

    asyncio.run(go())


def test_doctor_bot_config_grammar():
    one = asyncio.run(check_bot(DoctorDeps(settings(ADMIN_IDS="1", BOT_TOKEN=""))))
    assert one.text == "BOT_TOKEN not set" and one.fix == "Add it to .env (see .env.example)"
    both = asyncio.run(check_bot(DoctorDeps(settings(ADMIN_IDS="", BOT_TOKEN=""))))
    assert both.text == "BOT_TOKEN and ADMIN_IDS not set" and both.fix.startswith("Add them")


def test_user_visible_strings_have_no_internal_jargon():
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    helps = " ".join(sub.choices[name].format_help() for name in ("mock", "floors"))
    for word in ("FakeTapper", "sibling", "compatibility", "--interval"):
        assert word not in helps and word not in HELP
    assert "(dry and live)" not in HELP
