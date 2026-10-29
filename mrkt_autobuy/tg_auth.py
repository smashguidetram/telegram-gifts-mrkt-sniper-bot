"""Telegram Mini App initData for @mrkt via a Telethon user session.

Telethon is imported lazily so the rest of the package (tests, mock mode)
does not need a Telegram session at all.
"""

from __future__ import annotations

import logging
import time
from urllib.parse import unquote

from .config import ConfigError, Settings

log = logging.getLogger(__name__)


def mask(secret: str | None, keep: int = 8) -> str:
    """Truncate secrets for logs: never log full initData/tokens."""
    if not secret:
        return "<empty>"
    if len(secret) <= keep:
        return "***"
    return f"{secret[:keep]}...({len(secret)} chars)"


def extract_tg_web_app_data(url: str) -> str:
    """Take `tgWebAppData` out of a Mini App URL (fragment or query) and unquote it once.

    The result is the raw initData string expected by MRKT `POST /auth`
    (e.g. "query_id=...&user=%7B...%7D&auth_date=...&hash=...").
    """
    marker = "tgWebAppData="
    idx = url.find(marker)
    if idx == -1:
        raise ValueError("tgWebAppData not found in web app URL")
    value = url[idx + len(marker) :]
    # The value itself is percent-encoded, so its own '&' are %26: the first raw '&' ends it.
    value = value.split("&", 1)[0]
    data = unquote(value)
    if "hash=" not in data:
        raise ValueError("tgWebAppData does not look like initData (no hash)")
    return data


def _make_client(settings: Settings):
    settings.require_telegram_user()
    try:
        from telethon import TelegramClient
    except ImportError as exc:  # pragma: no cover
        raise ConfigError("telethon is not installed: pip install -r requirements.txt") from exc
    settings.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(str(settings.session_path), settings.TG_API_ID, settings.TG_API_HASH)


class TelegramSession:
    """One shared, lazily connected Telethon client (initData, Telegram valuation, wizard link lookup).

    Sharing avoids two clients locking the same .session SQLite file."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.TG_API_ID and self.settings.TG_API_HASH)

    async def client(self):
        if self._client is None:
            self._client = _make_client(self.settings)
        if not self._client.is_connected():
            await self._client.connect()
        if not await self._client.is_user_authorized():
            raise ConfigError(
                "Telegram session is not authorized. Run: python -m mrkt_autobuy login "
                f"(session file {self.settings.session_path}.session)"
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None


async def probe_session(settings: Settings, session: "TelegramSession | None" = None, timeout: float = 5.0):
    """(authorized, has_password) for /doctor. Never logs or returns the phone number."""
    import asyncio

    from telethon.tl.functions.account import GetPasswordRequest

    own = session is None
    session = session or TelegramSession(settings)
    try:
        client = await asyncio.wait_for(session.client(), timeout)
    except ConfigError:
        return False, None
    try:
        pwd = await asyncio.wait_for(client(GetPasswordRequest()), timeout)
        return True, bool(getattr(pwd, "has_password", False))
    finally:
        if own:
            await session.close()


async def lookup_gift(session: "TelegramSession", slug: str, timeout: float = 5.0) -> dict | None:
    """payments.getUniqueStarGift(slug) -> {collection, number, model, backdrop, symbol} for the wizard."""
    import asyncio

    from telethon.tl.functions.payments import GetUniqueStarGiftRequest

    client = await session.client()
    res = await asyncio.wait_for(client(GetUniqueStarGiftRequest(slug=slug)), timeout)
    gift = getattr(res, "gift", None)
    if gift is None:
        return None
    out = {"collection": getattr(gift, "title", None), "number": getattr(gift, "num", None)}
    for attr in getattr(gift, "attributes", []) or []:
        kind = type(attr).__name__  # StarGiftAttributeModel / Backdrop / Pattern
        name = getattr(attr, "name", None)
        if kind.endswith("Model"):
            out["model"] = name
        elif kind.endswith("Backdrop"):
            out["backdrop"] = name
        elif kind.endswith("Pattern"):
            out["symbol"] = name
    return out


async def request_init_data(settings: Settings, session: "TelegramSession | None" = None) -> str:
    """Open the @mrkt Mini App with the logged-in user account and return initData."""
    if session is not None:
        return await _request_init_data(settings, await session.client())
    client = _make_client(settings)
    await client.connect()
    try:
        return await _request_init_data(settings, client)
    finally:
        await client.disconnect()


async def _request_init_data(settings: Settings, client) -> str:
    from telethon import functions, types, utils

    if not await client.is_user_authorized():
        raise ConfigError(
            "Telegram session is not authorized. Run: python -m mrkt_autobuy login "
            f"(session file {settings.session_path}.session)"
        )
    bot_peer = await client.get_input_entity(settings.MRKT_BOT_USERNAME)
    try:
        # TODO(verify): app short name "app" for @mrkt (taken from community clients).
        result = await client(
            functions.messages.RequestAppWebViewRequest(
                peer=bot_peer,
                app=types.InputBotAppShortName(
                    bot_id=utils.get_input_user(bot_peer),
                    short_name=settings.MRKT_APP_SHORT_NAME,
                ),
                platform="android",
                write_allowed=True,
            )
        )
    except Exception as exc:  # fall back to the plain web view by URL
        log.warning("RequestAppWebView failed (%s), falling back to RequestWebView", exc)
        result = await client(
            functions.messages.RequestWebViewRequest(
                peer=bot_peer,
                bot=utils.get_input_user(bot_peer),
                platform="android",
                url=settings.MRKT_WEBAPP_URL,
                from_bot_menu=False,
            )
        )
    init_data = extract_tg_web_app_data(result.url)
    log.info("Got initData %s", mask(init_data, 16))
    return init_data


async def interactive_login(settings: Settings) -> None:
    """First-time phone/code (and 2FA) login; stores data/<TG_SESSION_NAME>.session."""
    client = _make_client(settings)
    await client.start()  # prompts for phone, code and password in the terminal
    me = await client.get_me()
    print(f"Logged in as {getattr(me, 'username', None) or me.id}. Session: {settings.session_path}.session")
    await client.disconnect()


class InitDataProvider:
    """Caches initData; refreshes after INIT_DATA_TTL or when forced (401/403)."""

    def __init__(self, settings: Settings, session: TelegramSession | None = None) -> None:
        self.settings = settings
        self.session = session
        self._value: str | None = None
        self._fetched_at = 0.0

    @property
    def age(self) -> float | None:
        return None if self._value is None else time.monotonic() - self._fetched_at

    async def get(self, force: bool = False) -> str:
        fresh = self._value and (time.monotonic() - self._fetched_at) < self.settings.INIT_DATA_TTL
        if force or not fresh:
            self._value = await request_init_data(self.settings, self.session)
            self._fetched_at = time.monotonic()
        assert self._value is not None
        return self._value
