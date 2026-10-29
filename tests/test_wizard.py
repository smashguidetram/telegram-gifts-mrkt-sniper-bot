import asyncio
import os
from datetime import datetime, timezone

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from helpers import ADMIN, NOW, Clock, demo_engine, settings

from mrkt_autobuy.bot import AppContext, build_dispatcher
from mrkt_autobuy.client import MockClient, parse_collection_floors
from mrkt_autobuy.doctor import Check, DoctorDeps, check_2fa, check_session, check_session_mode, format_doctor, run_doctor
from mrkt_autobuy.models import ton_to_nano
from mrkt_autobuy.notifier import keyboard_callbacks
from mrkt_autobuy.pricing import FeeModel
from mrkt_autobuy.rules import BuyRule
from mrkt_autobuy.wizard import (
    all_wizard_keyboards,
    collection_options,
    fuzzy_matches,
    max_options,
    parse_nft_link,
    preview_rule,
)

NAMES = ["Astral Shard", "B-Day Candle", "Berry Box", "Bunny Muffin", "Candy Cane", "Desk Calendar", "Durov's Cap",
         "Eternal Rose", "Genie Lamp", "Hex Pot", "Jelly Bunny", "Lunar Snake", "Plush Pepe"]


class WizardClient(MockClient):
    """Demo book plus 13 collections so the wizard needs a second page."""

    async def collection_floors(self):
        self.requests += 1
        raw = [{"name": n, "title": n, "floorPriceNanoTons": ton_to_nano(1.0 + i)} for i, n in enumerate(NAMES)]
        return parse_collection_floors(raw)


class FakeSession(BaseSession):
    """Answers Bot API calls locally and records them (no network)."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = []
        self._id = 100

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            self._id += 1
            return Message(message_id=self._id, date=datetime.now(timezone.utc),
                           chat=Chat(id=getattr(method, "chat_id", None) or ADMIN, type="private"),
                           text=method.text, reply_markup=method.reply_markup)
        return True

    async def close(self) -> None:
        return None

    async def stream_content(self, *args, **kwargs):
        yield b""


def _user(uid=ADMIN):
    return User(id=uid, is_bot=False, first_name="Admin")


class Driver:
    def __init__(self, dp, bot, session):
        self.dp, self.bot, self.session = dp, bot, session
        self.n = 0

    def last_markup(self):
        for call in reversed(self.session.calls):
            if isinstance(call, (SendMessage, EditMessageText)) and call.reply_markup is not None:
                return call.reply_markup
        return None

    def last_text(self):
        for call in reversed(self.session.calls):
            if isinstance(call, (SendMessage, EditMessageText)):
                return call.text
        return ""

    async def text(self, text, uid=ADMIN):
        self.n += 1
        msg = Message(message_id=self.n, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                      from_user=_user(uid), text=text)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, message=msg))

    async def tap(self, label_start, uid=ADMIN):
        markup = self.last_markup()
        button = next(b for row in markup.inline_keyboard for b in row if b.text.startswith(label_start))
        self.n += 1
        msg = Message(message_id=500, date=datetime.now(timezone.utc), chat=Chat(id=uid, type="private"),
                      from_user=_user(), text="card")
        cb = CallbackQuery(id=str(self.n), from_user=_user(uid), chat_instance="x", message=msg, data=button.callback_data)
        await self.dp.feed_update(self.bot, Update(update_id=self.n, callback_query=cb))


async def _wizard(s=None):
    clock = Clock()
    engine, storage, client, notifier, _ = await demo_engine(s or settings(), rules=[], client=WizardClient("demo", now=NOW),
                                                             clock=clock)
    ctx = AppContext(engine.settings, storage, client, engine)
    session = FakeSession()
    bot = Bot(token="42:TEST", session=session)
    return Driver(build_dispatcher(ctx), bot, session), storage


def test_scripted_wizard_saves_a_confirm_rule_in_six_taps():
    async def go():
        d, storage = await _wizard(settings(MAX_PRICE_TON=100, DAILY_BUDGET_TON=200))
        try:
            await d.text("/hunt")
            assert "Step 1/5" in d.last_text()
            await d.tap("›")  # page 2
            await d.tap("Lunar Snake")  # 1
            assert "Step 2/5" in d.last_text()
            await d.tap("Any model")  # 2
            await d.tap("≥ 0.5")  # 3
            await d.tap("floor×0.9")  # 4
            await d.tap("Ask me first")  # 5
            summary = d.last_text()
            assert summary.startswith("Hunt Lunar Snake (any model). Tell me when I'd make at least 0.50 TON after fees")
            assert "Ask before buying. Practice mode." in summary
            assert "No history yet" in summary or "In the last 24 h" in summary
            assert await storage.list_rules() == []  # nothing saved before Save
            await d.tap("Save")  # 6
            [rule] = await storage.list_rules()
            assert (rule.collection, rule.model, rule.mode, rule.source) == ("Lunar Snake", None, "confirm", "bot")
            assert rule.min_profit_ton == 0.5 and rule.max_price_ton == pytest.approx(12 * 0.9)
            assert rule.state == "shadow"
        finally:
            await storage.close()

    asyncio.run(go())


def test_cancel_saves_nothing_and_non_admin_is_refused():
    async def go():
        d, storage = await _wizard()
        try:
            await d.text("/hunt")
            await d.tap("Desk Calendar")
            await d.tap("Cancel")
            assert "Nothing was saved" in d.last_text()
            assert await storage.list_rules() == []
            await d.text("/hunt")
            await d.tap("Desk Calendar", uid=999)  # someone else presses the button
            answers = [c for c in d.session.calls if isinstance(c, AnswerCallbackQuery)]
            assert answers[-1].text == "Not allowed"
        finally:
            await storage.close()

    asyncio.run(go())


def test_typed_name_and_pasted_link():
    async def go():
        d, storage = await _wizard()
        try:
            await d.text("/hunt")
            await d.text("plush pep")
            assert "Did you mean" in d.last_text()
            labels = [b.text for row in d.last_markup().inline_keyboard for b in row]
            assert labels[0] == "Plush Pepe"
            await d.text("https://t.me/nft/LunarSnake-4410")
            assert "Step 2/5 · Lunar Snake" in d.last_text()
        finally:
            await storage.close()

    asyncio.run(go())


def test_max_price_capped_by_max_price_ton():
    async def go():
        d, storage = await _wizard(settings(MAX_PRICE_TON=5))
        try:
            await d.text("/hunt")
            await d.tap("›")
            await d.tap("Plush Pepe")  # floor 13
            await d.tap("Any model")
            await d.tap("Under floor by 10%")
            assert "capped at MAX_PRICE_TON=5" in d.last_text() or "MAX_PRICE_TON=5" in d.last_text()
            await d.tap("floor×1")
            await d.tap("Just alert me")
            await d.tap("Save")
            [rule] = await storage.list_rules()
            assert rule.max_price_ton == 5 and rule.mode == "watch" and rule.min_discount_pct == 10
        finally:
            await storage.close()

    asyncio.run(go())
    opts = max_options(1500.0, 1350.0)
    assert [o[1] for o in opts] == [1200.0, 1350.0, 1350.0] and opts[2][2]


def test_every_wizard_callback_fits_64_bytes():
    datas = [d for kb in all_wizard_keyboards() for d in keyboard_callbacks(kb)]
    assert datas and all(len(d.encode()) <= 64 for d in datas)


def test_fuzzy_match():
    assert fuzzy_matches("plush pep", NAMES)[0] == "Plush Pepe"
    assert fuzzy_matches("zzzz", NAMES) == []


def test_nft_link_parsing():
    assert parse_nft_link("https://t.me/nft/PlushPepe-123") == ("PlushPepe", 123)
    assert parse_nft_link("t.me/nft/DurovsCap-5") == ("DurovsCap", 5)
    assert parse_nft_link("hello world") is None
    assert parse_nft_link("https://t.me/mrkt") is None
    opts = collection_options({"Durov's Cap": 1.0, "DurovsCap": 1.0, "Plush Pepe": 2.0})
    assert [o[0] for o in opts] == ["Durov's Cap", "Plush Pepe"]


def test_preview_rule_counts_on_fixture_tape():
    def obs(lid, price, hours_ago, model="Albino"):
        t = NOW - hours_ago * 3600
        return {"listing_id": lid, "price_ton": price, "collection": "Lunar Snake", "model": model, "backdrop": None,
                "symbol": None, "number": 1, "first_seen": t, "last_seen": t}

    tape = [obs("a", 1.10, 20), obs("b", 1.65, 18), obs("c", 1.05, 6), obs("d", 0.90, 4), obs("old", 0.5, 30)]
    snaps = {"lunarsnake|albino||": [(NOW - 24 * 3600, 1.50, 4, "cluster"), (NOW - 12 * 3600, 1.52, 4, "cluster")]}
    rule = BuyRule("Lunar Snake", 3, min_profit_ton=0.3)
    p = preview_rule(rule, tape, snaps, FeeModel(0, 5, 0), NOW, 24, locked_ids={"d"})
    assert p.alerts == 2 and p.locked == 1 and not p.approximate
    assert p.text == "In the last 24 h this would have alerted 2× (avg −29%, best +0.4 TON net); 1 was locked."
    short = preview_rule(rule, tape[:4], snaps, FeeModel(0, 5, 0), NOW, 24)
    assert short.text.startswith("I've only been watching for 20 h.")
    assert preview_rule(BuyRule("Hex Pot", 3), tape, snaps, FeeModel(), NOW).text.startswith("No history yet")
    approx = preview_rule(BuyRule("Lunar Snake", 3), [obs("z", 1.0, 2, model="Viper")], {}, FeeModel(), NOW,
                          current_floor=1.5, tape_start=NOW - 48 * 3600)
    assert approx.approximate and "Approximate" in approx.text


# --- /doctor with injected fakes --------------------------------------------------------------


def test_doctor_checks_with_fakes(tmp_path):
    session_file = tmp_path / "mrkt.session"
    session_file.write_text("x")
    os.chmod(session_file, 0o644)

    async def no_2fa():
        return True, False

    async def go():
        s = settings(TG_API_ID=1, TG_API_HASH="h")
        deps = DoctorDeps(s, None, no_2fa, session_file, now=NOW)
        fa = await check_2fa(deps)
        assert fa.status == "fail" and fa.fix == "Telegram → Settings → Privacy and Security → Two-Step Verification"
        mode = await check_session_mode(deps)
        if os.name == "nt":
            # Windows has no POSIX modes; the check reports the profile-private default.
            assert mode.status == "ok"
        else:
            assert mode.status == "warn" and mode.fix == "chmod 600 data/*.session"
            os.chmod(session_file, 0o600)
            assert (await check_session_mode(deps)).status == "ok"
        assert (await check_session(deps)).status == "ok"
        missing = DoctorDeps(s, None, no_2fa, tmp_path / "none.session", now=NOW)
        sess = await check_session(missing)
        assert sess.status == "fail" and sess.fix == "python -m mrkt_autobuy login"

    asyncio.run(go())


def test_doctor_without_credentials_shows_fail_lines_not_a_crash():
    async def go():
        engine, storage, client, notifier, clock = await demo_engine(settings(ADMIN_IDS="", BOT_TOKEN=""))
        try:
            started = asyncio.get_running_loop().time()
            checks = await run_doctor(DoctorDeps(engine.settings, engine, None, None, now=clock()))
            assert asyncio.get_running_loop().time() - started < 10
            text = format_doctor(checks)
            assert "✗ Bot config" in text and "✗ Telegram session: not logged in" in text
            assert "! Fees are assumptions" in text
            assert len(checks) >= 12 and all(isinstance(c, Check) and (c.status == "ok" or c.fix or c.status == "warn")
                                             for c in checks)
            assert "Market reachable (120 ms)" in text
        finally:
            await storage.close()

    asyncio.run(go())
