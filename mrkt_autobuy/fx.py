"""TON -> USD rate (tonapi, keyless, cached). Never blocks a decision: failure means 'no rate'."""

from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

log = logging.getLogger(__name__)

Fetch = Callable[[str], Awaitable[Any]]


def parse_tonapi_rate(data: Any, currency: str = "USD") -> float | None:
    """{"rates": {"TON": {"prices": {"USD": 1.246}}}} -> 1.246 (verified shape, 2026-10-28)."""
    try:
        value = float(data["rates"]["TON"]["prices"][currency.upper()])
    except (KeyError, TypeError, ValueError):
        return None
    return value if value > 0 else None


async def _httpx_get_json(url: str) -> Any:
    import httpx

    async with httpx.AsyncClient(timeout=3.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return resp.json()


class TonRate:
    """USD per TON. `fixed` pins the rate (mock); otherwise GET TON_RATE_URL every `ttl` seconds."""

    def __init__(self, url: str, ttl: float = 600, *, fixed: float | None = None, fetch: Fetch | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.url = url
        self.ttl = ttl
        self.fixed = fixed
        self._fetch = fetch or _httpx_get_json
        self._clock = clock
        self._value: float | None = fixed
        self._at = 0.0
        self.last_error: str | None = None

    @property
    def value(self) -> float | None:
        return self._value

    async def usd_per_ton(self) -> float | None:
        if self.fixed is not None:
            return self.fixed
        now = self._clock()
        if self._value is not None and now - self._at < self.ttl:
            return self._value
        if self.last_error and now - self._at < 60:  # do not hammer a failing endpoint
            return self._value
        try:
            rate = parse_tonapi_rate(await self._fetch(self.url))
            self.last_error = None if rate else "unexpected response"
            if rate:
                self._value = rate
        except Exception as exc:  # network, JSON, HTTP status
            self.last_error = f"{type(exc).__name__}"
            log.debug("TON rate failed: %s", exc)
        self._at = now
        return self._value

    def status(self) -> str:
        if self.fixed is not None:
            return f"fixed {self.fixed:g} USD/TON"
        if self._value is None:
            return f"n/a ({self.last_error or 'not fetched yet'})"
        return f"{self._value:.3f} USD/TON" + (f" (last refresh failed: {self.last_error})" if self.last_error else "")


def usd(amount_ton: float | None, rate: float | None) -> str:
    if amount_ton is None or not rate:
        return ""
    return f"≈ ${amount_ton * rate:,.2f}"
