"""Application settings loaded from environment / .env (pydantic-settings)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or invalid (names the .env key)."""


class Settings(BaseSettings):
    # allow_inf_nan=False: "nan" or "inf" for a limit would make every comparison False and switch it off.
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", env_ignore_empty=True, allow_inf_nan=False
    )

    # --- Admin bot (aiogram) ---
    BOT_TOKEN: str = ""
    ADMIN_IDS: str = ""  # comma-separated Telegram user ids

    # --- Telegram user account (Telethon, only to obtain Mini App initData) ---
    TG_API_ID: int = 0
    TG_API_HASH: str = ""
    TG_SESSION_NAME: str = "mrkt"
    MRKT_BOT_USERNAME: str = "mrkt"
    MRKT_APP_SHORT_NAME: str = "app"
    MRKT_WEBAPP_URL: str = "https://cdn.tgmrkt.io/"
    INIT_DATA_TTL: int = 3000  # seconds before initData is re-requested

    # --- MRKT API ---
    MRKT_API_BASE: str = "https://api.tgmrkt.io/api/v1"
    MRKT_TOKEN: str = ""  # optional: pre-obtained token, skips Telethon until it expires
    HTTP_BACKEND: str = "curl_cffi"  # curl_cffi | httpx
    HTTP_PROXY: str = ""
    HTTP_TIMEOUT: float = 10.0

    # --- Safety / limits ---
    DRY_RUN: bool = True
    MAX_PRICE_TON: float = 10.0
    DAILY_BUDGET_TON: float = 50.0
    MAX_BUYS_PER_HOUR: int = 3

    # --- Live gate, practice ledger and breakers ---
    LIVE_ARM_HOURS: float = 24.0  # LIVE lasts this long after going live (/dry off), then flips back to practice
    PAPER_HORIZON_DAYS: int = 7
    MARK_MAX_AGE_SEC: int = 1800
    MARK_MAX_QUERIES: int = 10
    MARK_INTERVAL_SEC: int = 3600
    GATE_MIN_TRADES: int = 5
    GATE_DAYS: int = 7
    CRASH_PCT: float = 20.0
    CRASH_PAUSE_MIN: int = 60
    LOSS_STREAK_N: int = 3
    LOSS_STREAK_PCT: float = 10.0
    BALANCE_GUARD: bool = True
    BALANCE_TOLERANCE_TON: float = 0.5
    AUTO_PAUSE_ERRORS: int = 5  # consecutive failed live buys -> pause
    STALE_DATA_SEC: int = 300  # warn once when no cycle succeeded for this long
    CONFIRM_ABOVE_TON: float = 0.0  # buys costing more always ask first; 0 = 0.5 x MAX_PRICE_TON
    HOT_WALLET_WARN_X: float = 3.0
    DIGEST_HOUR: int = -1  # local hour for the daily digest, -1 = off
    TZ_OFFSET_HOURS: int = 3

    # --- Fees (net math). TODO(verify): MRKT fee schedule; defaults are assumptions ---
    BUY_FEE_PCT: float = 0.0
    SELL_FEE_PCT: float = 5.0
    WITHDRAW_FEE_TON: float = 0.0
    FEES_VERIFIED: bool = False

    # --- Robust floor and trap filter ---
    CLUSTER_K: int = 2
    CLUSTER_BAND_PCT: float = 10.0
    MIN_COMPARABLES_COLLECTION: int = 5
    MIN_COMPARABLES_ATTR: int = 3
    FLOOR_JUMP_PCT: float = 25.0
    TOO_GOOD_PCT: float = 25.0
    OBS_TTL_DAYS: int = 14
    SNAPSHOT_TTL_DAYS: int = 14
    DECISIONS_TTL_DAYS: int = 7

    # --- Deal cards ---
    CONFIRM_TTL_SEC: int = 90
    NEAR_MISS_PCT: float = 5.0
    NEAR_MISS_MAX_PER_HOUR: int = 3
    PREVIEW_HOURS: int = 24
    DISPLAY_CURRENCY: str = "usd"  # usd | none

    # --- Second opinion: Telegram valuation and other markets ---
    PEERS_TIMEOUT_SEC: float = 3.0
    TG_VALUE_ENABLED: bool = True
    TG_VALUE_TTL: int = 600
    TG_VALUE_MAX_PER_MIN: int = 10
    TG_VALUE_GUARD: bool = True
    TON_RATE_URL: str = "https://tonapi.io/v2/rates?tokens=ton&currencies=usd"
    TON_RATE_TTL: int = 600
    PEER_FLOOR_FILES: str = ""  # comma-separated floors_<market>.json paths written by your other bots
    PEER_MAX_AGE_SEC: int = 600
    PEER_PORTALS_PUBLIC: bool = False
    PORTALS_PUBLIC_URL: str = "https://portal-market.com/api/collections?limit=500"
    PORTALS_PUBLIC_TTL: int = 300
    CHEAPER_ELSEWHERE_PCT: float = 3.0

    # --- Shadow rules, events, digest, optional LLM ---
    SHADOW_NEW_RULES: bool = True
    SHADOW_MIN_HOURS: float = 24.0
    SHADOW_MIN_MATCHES: int = 5
    SHADOW_MAX_HOURS: float = 168.0
    SNIPED_WINDOW_SEC: int = 120
    EVENTS_FILE: str = "data/events.jsonl"
    EVENTS_MAX_MB: float = 20.0
    PEER_EVENT_FILES: str = ""
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-5-5"

    # --- Polling ---
    # TODO(verify): MRKT's rate limit. Each cycle makes 2 requests per rule group (+1 floors request about once a
    # minute) and MRKT has answered 429 before; with many rules raise POLL_INTERVAL or set SCAN_NEWEST=false.
    POLL_INTERVAL: float = 2.0
    MAX_BACKOFF: float = 60.0
    PAGE_SIZE: int = 20
    SCAN_NEWEST: bool = True
    FLOOR_CACHE_TTL: float = 60.0

    # --- Storage / misc ---
    DB_PATH: str = "data/mrkt_autobuy.sqlite3"
    DATA_DIR: str = "data"
    RULES_FILE: str = "rules.json"
    LOG_LEVEL: str = "INFO"

    @property
    def admin_ids(self) -> list[int]:
        ids: list[int] = []
        for part in self.ADMIN_IDS.replace(";", ",").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError as exc:
                raise ConfigError(f"ADMIN_IDS must be comma-separated integers, got {part!r}") from exc
        return ids

    @property
    def floors_ttl(self) -> float:
        """How often collection floors refresh and floor snapshots are written (FLOOR_CACHE_TTL)."""
        return self.FLOOR_CACHE_TTL

    @property
    def confirm_above_ton(self) -> float:
        return self.CONFIRM_ABOVE_TON if self.CONFIRM_ABOVE_TON > 0 else 0.5 * self.MAX_PRICE_TON

    @staticmethod
    def _paths(value: str) -> list[str]:
        return [p.strip() for p in value.split(",") if p.strip()]

    @property
    def peer_floor_files(self) -> list[str]:
        return self._paths(self.PEER_FLOOR_FILES)

    @property
    def peer_event_files(self) -> list[str]:
        return self._paths(self.PEER_EVENT_FILES)

    @property
    def session_path(self) -> Path:
        return Path(self.DATA_DIR) / self.TG_SESSION_NAME

    def require(self, *keys: str) -> None:
        """Raise ConfigError naming the first missing/empty key."""
        for key in keys:
            value = getattr(self, key)
            if value in ("", 0, None):
                raise ConfigError(f"{key} is not set. Add it to .env (see .env.example).")

    def require_telegram_user(self) -> None:
        self.require("TG_API_ID", "TG_API_HASH")

    def require_bot(self) -> None:
        self.require("BOT_TOKEN", "ADMIN_IDS")
        if not self.admin_ids:
            raise ConfigError("ADMIN_IDS is empty. Add your Telegram user id to .env.")


def load_settings() -> Settings:
    return Settings()
