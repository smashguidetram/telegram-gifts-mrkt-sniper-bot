"""MRKT API client (api.tgmrkt.io) + offline MockClient.

Everything API-specific lives here. Endpoints and payloads were taken from the
MRKT web app bundle (cdn.tgmrkt.io, release 52117, Oct 2026) and community
clients (amrkt, mrkts); items still marked TODO(verify) were not exercised
against the live API with a real account.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

from .config import ConfigError, Settings
from .models import Listing, nano_to_ton, norm, ton_to_nano
from .rules import BuyRule
from .tg_auth import InitDataProvider, mask

log = logging.getLogger(__name__)

ORDER_PRICE = "Price"
# The web app's default market ordering is "None" with lowToHigh=false, which lists
# the most recently listed gifts first. Enum seen in the bundle:
# None | Price | ModelRarity | BackgroundRarity | SymbolRarity | Number.
ORDER_NEWEST = "None"  # TODO(verify): confirm "None"+lowToHigh=false really means newest-first

DEFAULT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://cdn.tgmrkt.io",
    "Referer": "https://cdn.tgmrkt.io/",
    "Accept-Language": "en-US,en;q=0.9",
}
# TODO(verify): whether /gifts/saling accepts luckyBuy=false to exclude lucky-buy tickets. Until then
# the filter stays null and pricing.classify() rejects lucky* listings client-side (fail closed).
LUCKY_FILTER: bool | None = None

# Endpoints that exist for sure: a 404 there means "not authenticated".
AUTH_SENSITIVE_PATHS = {"/gifts/saling", "/gifts/collections", "/balance"}
HTTPX_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36"
)


# --- errors -------------------------------------------------------------------


class ApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


class RateLimited(ApiError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(429, message)
        self.retry_after = retry_after


class AuthError(ApiError):
    pass


# --- pure helpers (unit-tested) -------------------------------------------------


@dataclass(slots=True)
class SearchFilters:
    collections: list[str] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    backdrops: list[str] = field(default_factory=list)
    symbols: list[str] = field(default_factory=list)
    ordering: str = ORDER_PRICE
    low_to_high: bool = True
    count: int = 20
    cursor: str = ""
    min_price_ton: float | None = None
    max_price_ton: float | None = None
    number: int | None = None

    @classmethod
    def for_rule(cls, rule: BuyRule, *, newest: bool = False, count: int = 20) -> "SearchFilters":
        return cls(
            collections=[rule.collection],
            models=[rule.model] if rule.model else [],
            backdrops=[rule.backdrop] if rule.backdrop else [],
            symbols=[rule.symbol] if rule.symbol else [],
            ordering=ORDER_NEWEST if newest else ORDER_PRICE,
            low_to_high=not newest,
            count=count,
        )


def build_saling_payload(f: SearchFilters) -> dict[str, Any]:
    """Body for POST /gifts/saling, mirroring the web app's default filter object."""
    return {
        "count": f.count,
        "cursor": f.cursor,
        # TODO(verify): collectionNames takes the `name` from /gifts/collections; a rule's string is sent as is.
        "collectionNames": list(f.collections),
        "modelNames": list(f.models),
        "backdropNames": list(f.backdrops),
        "symbolNames": list(f.symbols),
        "minPrice": ton_to_nano(f.min_price_ton) if f.min_price_ton is not None else None,
        "maxPrice": ton_to_nano(f.max_price_ton) if f.max_price_ton is not None else None,
        "number": f.number,  # TODO(verify): single-gift lookup by number (used by get_listing)
        "isPremarket": None,  # premarket items are kept; pricing.classify() flags them LOCKED
        "isNew": None,
        "luckyBuy": LUCKY_FILTER,
        "luckybuyCashback": None,
        "giftType": None,
        "craftable": None,
        "isCrafted": None,
        "tgCanBeCraftedFrom": None,
        "removeSelfSales": True,  # never "snipe" our own listings
        "isTransferable": None,
        "availableForStaking": None,
        "forGame": None,
        "ordering": f.ordering,
        "lowToHigh": f.low_to_high,
        "query": None,
    }


def build_buy_payload(listing: Listing) -> dict[str, Any]:
    """Body for POST /gifts/buy exactly as the web app sends it: ids + expected price per id."""
    price = listing.raw.get("salePrice")
    nano = int(price) if price not in (None, "") else ton_to_nano(listing.price_ton)
    return {"ids": [listing.id], "prices": {listing.id: nano}}


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_listing(raw: dict[str, Any]) -> Listing:
    """Map one gift object from /gifts/saling to Listing. Prices are nanoTON.

    TODO(verify): floorPriceNanoTONsByBackdropModel in the gift object could give a more precise floor for
    model or backdrop rules; its shape is unverified, so it is not used yet."""
    gid = raw.get("id")
    if not gid:
        raise ValueError("gift without id")
    number = raw.get("number")
    return Listing(
        id=str(gid),
        collection=raw.get("collectionName") or raw.get("collectionTitle") or raw.get("title") or "",
        model=raw.get("modelName") or raw.get("modelTitle"),
        backdrop=raw.get("backdropName"),
        symbol=raw.get("symbolName"),
        number=int(number) if isinstance(number, (int, str)) and str(number).isdigit() else None,
        price_ton=nano_to_ton(raw.get("salePrice")),
        # TODO(verify): no explicit "listed at" field seen yet; these are best guesses.
        listed_at=_parse_dt(raw.get("listedAt") or raw.get("saleDate") or raw.get("updatedAt")),
        raw=raw,
    )


def parse_listings(data: Any) -> list[Listing]:
    gifts = data.get("gifts", []) if isinstance(data, dict) else (data or [])
    out: list[Listing] = []
    for raw in gifts:
        try:
            out.append(parse_listing(raw))
        except (ValueError, TypeError) as exc:
            log.debug("skip unparsable gift %s: %s", str(raw)[:120], exc)
    return out


def parse_collection_floors(data: Any) -> dict[str, float]:
    """GET /gifts/collections -> {collection name: floor TON} (title added as alias)."""
    floors: dict[str, float] = {}
    for item in data or []:
        if not isinstance(item, dict):
            continue
        nano = item.get("floorPriceNanoTons", item.get("floorPriceNanoTONs"))
        if nano in (None, 0):
            continue
        floor = nano_to_ton(nano)
        for key in (item.get("name"), item.get("title")):
            if key:
                floors[str(key)] = floor
    return floors


def parse_balance(data: Any) -> float:
    """GET /balance -> TON. `hard` is the TON balance in nanoTON."""
    if isinstance(data, dict):
        return nano_to_ton(data.get("hard", 0))  # TODO(verify): hard vs totalHard/hardLocked
    return 0.0


@dataclass(slots=True)
class BuyResult:
    ok: bool
    detail: str
    raw: Any = None
    uncertain: bool = False  # not ok, but the order may have gone through: recorded 'unknown', counts toward limits


def parse_buy_response(data: Any, listing_id: str) -> BuyResult:
    """Classify a 2xx POST /gifts/buy body. Fails closed for the limits: only a non-empty list is a buy.
    Any other 2xx body (an empty list, a dict, no body) may be a purchase in a shape we do not know, so it is
    uncertain: recorded as 'unknown', counted toward DAILY_BUDGET_TON and MAX_BUYS_PER_HOUR. A refusal that
    arrives as HTTP 4xx raises ApiError and is a definite failure.
    TODO(verify): the success shape; community clients observed a list of {userGift, price}."""
    if isinstance(data, list) and data:
        return BuyResult(True, f"purchased {len(data)} item(s)", data)
    if isinstance(data, list):
        what = "empty response (already sold?)"
    elif isinstance(data, dict) and (data.get("error") or data.get("message")):
        what = str(data.get("error") or data.get("message"))[:200]
    elif data is None:
        what = "no body"
    else:
        what = f"unrecognized buy response {json.dumps(data, default=str)[:200]}"
    return BuyResult(False, f"{what} (result unknown: counted toward your limits, check MRKT)", data, uncertain=True)


# --- client protocol ---------------------------------------------------------------


class MarketClient(Protocol):
    name: str

    async def start(self) -> None: ...
    async def close(self) -> None: ...
    async def search_listings(self, filters: SearchFilters) -> list[Listing]: ...
    async def collection_floors(self) -> dict[str, float]: ...
    async def buy(self, listing: Listing) -> BuyResult: ...
    async def balance(self) -> float: ...
    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None: ...


def find_listing(listings: list[Listing], listing_id: str) -> Listing | None:
    return next((l for l in listings if l.id == listing_id), None)


# --- HTTP backend ------------------------------------------------------------------


class _Http:
    """Thin wrapper so curl_cffi (Cloudflare-friendly TLS) and httpx are interchangeable."""

    def __init__(self, settings: Settings) -> None:
        self.kind = settings.HTTP_BACKEND.lower()
        proxy = settings.HTTP_PROXY or None
        self._session: Any
        if self.kind == "curl_cffi":
            try:
                from curl_cffi.requests import AsyncSession

                self._session = AsyncSession(impersonate="chrome", proxy=proxy, timeout=settings.HTTP_TIMEOUT)
                return
            except ImportError:
                log.warning("curl_cffi not installed, falling back to httpx")
                self.kind = "httpx"
        import httpx

        self._session = httpx.AsyncClient(
            timeout=settings.HTTP_TIMEOUT, proxy=proxy, headers={"User-Agent": HTTPX_USER_AGENT}
        )

    async def request(self, method: str, url: str, *, json: Any = None, headers: dict[str, str]):
        try:
            return await self._session.request(method, url, json=json, headers=headers)
        except Exception as exc:  # network / TLS / timeout from either backend
            raise ApiError(0, f"network error: {type(exc).__name__}: {exc}") from exc

    async def close(self) -> None:
        if self.kind == "curl_cffi":
            await self._session.close()
        else:
            await self._session.aclose()


# --- real client -------------------------------------------------------------------


class MrktClient:
    name = "mrkt"

    def __init__(self, settings: Settings, init_data: InitDataProvider | None = None) -> None:
        self.settings = settings
        self.base = settings.MRKT_API_BASE.rstrip("/")
        self._init = init_data
        self._token: str | None = settings.MRKT_TOKEN or None
        self._http: _Http | None = None
        self._auth_lock = asyncio.Lock()
        self._last_404_reauth = 0.0
        # Clock-skew and latency info for /doctor (Date header of the last response).
        self.last_server_date: float | None = None
        self.last_response_at: float | None = None
        self.last_latency_ms: float | None = None

    async def start(self) -> None:
        if not self._token and not (self.settings.TG_API_ID and self.settings.TG_API_HASH):
            raise ConfigError("TG_API_ID / TG_API_HASH are not set (or set MRKT_TOKEN). Add them to .env.")
        if self._init is None:
            self._init = InitDataProvider(self.settings)
        if self._http is None:
            self._http = _Http(self.settings)

    async def close(self) -> None:
        if self._http is not None:
            await self._http.close()
            self._http = None

    async def __aenter__(self) -> "MrktClient":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def _headers(self) -> dict[str, str]:
        headers = dict(DEFAULT_HEADERS)
        if self._token:
            # Web app sends the raw token (no "Bearer") and also keeps it in a cookie.
            headers["Authorization"] = self._token
            headers["Cookie"] = f"access_token={self._token}"
        return headers

    async def authenticate(self, force: bool = False) -> None:
        async with self._auth_lock:
            if self._token and not force:
                return
            if not (self.settings.TG_API_ID and self.settings.TG_API_HASH):
                raise ConfigError("MRKT token expired and TG_API_ID / TG_API_HASH are not set in .env.")
            assert self._init is not None and self._http is not None
            init_data = await self._init.get(force=force)
            # Web app body: {"data": initData, "photo"?: url, "appId": null}
            resp = await self._http.request(
                "POST", f"{self.base}/auth", json={"data": init_data, "appId": None}, headers=dict(DEFAULT_HEADERS)
            )
            if resp.status_code != 200:
                raise AuthError(resp.status_code, f"auth failed: {resp.text[:200]}")
            token = (resp.json() or {}).get("token")
            if not token:
                raise AuthError(resp.status_code, "auth response has no token")
            self._token = token
            log.info("MRKT auth OK, token %s", mask(token))

    def _is_auth_404(self, status: int, path: str) -> bool:
        """The API answers 404 (not 401) to requests with a missing/invalid token on endpoints
        that certainly exist (observed 2026-10-28). Re-auth on those, at most once a minute."""
        # TODO(verify): confirm an *expired* token also yields 404 rather than 401.
        if status != 404 or path not in AUTH_SENSITIVE_PATHS:
            return False
        if time.monotonic() - self._last_404_reauth < 60:
            return False
        self._last_404_reauth = time.monotonic()
        return True

    async def _request(self, method: str, path: str, json: Any = None, *, retry_auth: bool = True) -> Any:
        if self._http is None:
            await self.start()
        if not self._token:
            await self.authenticate()
        assert self._http is not None
        started = time.perf_counter()
        resp = await self._http.request(method, f"{self.base}{path}", json=json, headers=self._headers())
        self.last_latency_ms = (time.perf_counter() - started) * 1000
        self.last_response_at = time.time()
        try:
            date = resp.headers.get("date")
            self.last_server_date = parsedate_to_datetime(date).timestamp() if date else None
        except (TypeError, ValueError):
            self.last_server_date = None
        status = resp.status_code
        if retry_auth and (status in (401, 403) or self._is_auth_404(status, path)):
            log.warning("%s %s -> %s, re-authenticating", method, path, status)
            await self.authenticate(force=True)
            return await self._request(method, path, json, retry_auth=False)
        if status == 429:
            retry_after = resp.headers.get("retry-after")
            try:
                ra = float(retry_after) if retry_after else None
            except ValueError:
                ra = None
            raise RateLimited(resp.text[:200] or "rate limited", ra)
        if status in (401, 403):
            raise AuthError(status, resp.text[:200])
        if status >= 400:
            raise ApiError(status, resp.text[:300])
        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise ApiError(status, f"non-JSON response: {resp.text[:200]}") from exc

    async def search_listings(self, filters: SearchFilters) -> list[Listing]:
        data = await self._request("POST", "/gifts/saling", build_saling_payload(filters))
        return parse_listings(data)

    async def collection_floors(self) -> dict[str, float]:
        data = await self._request("GET", "/gifts/collections")
        return parse_collection_floors(data)

    async def buy(self, listing: Listing) -> BuyResult:
        payload = build_buy_payload(listing)
        log.warning("LIVE BUY %s for %.4f TON", listing.id, listing.price_ton)
        data = await self._request("POST", "/gifts/buy", payload)
        return parse_buy_response(data, listing.id)

    async def balance(self) -> float:
        return parse_balance(await self._request("GET", "/balance"))

    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None:
        """Re-fetch one listing before a confirmed buy: search its collection/model (and number), find the id.
        TODO(verify): no single-gift endpoint is known; a listing past the first page reads as gone."""
        if hint is None:
            return None
        filters = SearchFilters(
            collections=[hint.collection], models=[hint.model] if hint.model else [], number=hint.number,
            count=max(self.settings.PAGE_SIZE, 20),
        )
        return find_listing(await self.search_listings(filters), listing_id)


# --- mock client -------------------------------------------------------------------


def _mock_gift(gid: str, collection: str, number: int, ton: float, model: str, backdrop: str, symbol: str,
               listed: str, **extra: Any) -> dict[str, Any]:
    raw = {
        "id": gid,
        "collectionName": collection,
        "collectionTitle": collection,
        "number": number,
        "modelName": model,
        "backdropName": backdrop,
        "symbolName": symbol,
        "salePrice": ton_to_nano(ton),
        "salePriceWithoutFee": ton_to_nano(ton * 0.95),
        "isOnSale": True,
        "listedAt": listed,
    }
    raw.update(extra)
    return raw


MOCK_FLOORS_RAW = [
    {"name": "Lunar Snake", "title": "Lunar Snake", "floorPriceNanoTons": ton_to_nano(3.0)},
    {"name": "Desk Calendar", "title": "Desk Calendar", "floorPriceNanoTons": ton_to_nano(2.0)},
    {"name": "Plush Pepe", "title": "Plush Pepe", "floorPriceNanoTons": ton_to_nano(4200)},
]

MOCK_LISTINGS_INITIAL = [
    # Far below floor -> alert + DRY buy with the "Lunar Snake" demo rule.
    _mock_gift("mock-ls-1042", "Lunar Snake", 1042, 1.20, "Albino", "Onyx Black", "Star", "2026-10-28T10:00:05Z"),
    # Above the rule's max price -> rejected.
    _mock_gift("mock-ls-2210", "Lunar Snake", 2210, 2.95, "Cobra", "Ivory White", "Moon", "2026-10-28T09:58:00Z"),
    _mock_gift("mock-ls-3311", "Lunar Snake", 3311, 3.10, "Viper", "Mint Green", "Leaf", "2026-10-28T09:40:00Z"),
    # Above "Desk Calendar max=1.5" -> rejected.
    _mock_gift("mock-dc-555", "Desk Calendar", 555, 1.90, "Classic", "Sky Blue", "Pin", "2026-10-28T09:50:00Z"),
    _mock_gift("mock-dc-777", "Desk Calendar", 777, 2.05, "Retro", "Coral Red", "Clip", "2026-10-28T09:30:00Z"),
    # Only 7% below the API floor, rule wants 25% -> rejected.
    _mock_gift("mock-pp-12", "Plush Pepe", 12, 3900.0, "Pumpkin", "Black", "Crown", "2026-10-28T08:00:00Z"),
]

MOCK_LISTINGS_LATER = [
    # Appears after the first cycle: fresh listing far below floor -> second DRY buy.
    _mock_gift("mock-dc-901", "Desk Calendar", 901, 0.95, "Classic", "Sky Blue", "Pin", "2026-10-28T10:01:00Z"),
]

MOCK_RULES = [
    # The scaffold book is thin (two comparables per collection, no cluster of 3), so its floors are
    # low-confidence by design and these pipeline-test rules accept that.
    BuyRule(collection="Lunar Snake", max_price_ton=2.5, min_discount_pct=20, source="demo", min_confidence="low"),
    BuyRule(collection="Desk Calendar", max_price_ton=1.5, source="demo", min_confidence="low"),
    # Only the API floor is known for this lone listing, so the floor confidence is low by design.
    BuyRule(collection="Plush Pepe", max_price_ton=5000, min_discount_pct=25, source="demo", min_confidence="low"),
]


# --- demo scenario: the named fixtures F1-F8 used by `python -m mrkt_autobuy mock` ---------------

DEMO_FLOORS_RAW = [
    {"name": "Lunar Snake", "title": "Lunar Snake", "floorPriceNanoTons": ton_to_nano(0.97)},
    {"name": "Desk Calendar", "title": "Desk Calendar", "floorPriceNanoTons": ton_to_nano(2.20)},
    {"name": "Jelly Bunny", "title": "Jelly Bunny", "floorPriceNanoTons": ton_to_nano(1.10)},
    {"name": "Spy Agaric", "title": "Spy Agaric", "floorPriceNanoTons": ton_to_nano(5.00)},
    {"name": "Homemade Cake", "title": "Homemade Cake", "floorPriceNanoTons": ton_to_nano(3.00)},
    {"name": "Plush Pepe", "title": "Plush Pepe", "floorPriceNanoTons": ton_to_nano(4200)},
]


def demo_listings(now: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(initial book, listings revealed by advance()). `now` positions the premarket unlock date."""
    t = "2026-10-28T10:00:00Z"
    unlock = datetime.fromtimestamp(now + 6 * 86400 - 3600, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    g = _mock_gift
    initial = [
        # F1 'troll ask': one model at 1.00, 3.00, 1.60, 1.62, 1.65 -> cluster floor 1.60, not 3.00.
        g("ls-4410", "Lunar Snake", 4410, 1.00, "Albino", "Onyx Black", "Star", t),
        g("ls-4411", "Lunar Snake", 4411, 3.00, "Albino", "Ivory White", "Moon", t),
        g("ls-4413", "Lunar Snake", 4413, 1.60, "Albino", "Mint Green", "Leaf", t),
        g("ls-4414", "Lunar Snake", 4414, 1.62, "Albino", "Sky Blue", "Pin", t),
        g("ls-4415", "Lunar Snake", 4415, 1.65, "Albino", "Coral Red", "Clip", t),
        # F2 'loses after fees': 0.97 against a cluster floor of 1.00 with a 5% sell fee.
        g("ls-4412", "Lunar Snake", 4412, 0.97, "Cobra", "Onyx Black", "Moon", t),
        g("ls-4420", "Lunar Snake", 4420, 1.00, "Cobra", "Ivory White", "Star", t),
        g("ls-4421", "Lunar Snake", 4421, 1.02, "Cobra", "Mint Green", "Leaf", t),
        g("ls-4422", "Lunar Snake", 4422, 1.05, "Cobra", "Sky Blue", "Pin", t),
        # Confirm-mode deal (repriced before the tap) and a near miss over the rule's max.
        g("dc-3002", "Desk Calendar", 3002, 2.20, "Classic", "Sky Blue", "Pin", t),
        g("dc-3001", "Desk Calendar", 3001, 2.62, "Classic", "Coral Red", "Clip", t),
        *[g(f"dc-30{10 + i}", "Desk Calendar", 3010 + i, p, "Retro", "Onyx Black", "Star", t)
          for i, p in enumerate((3.20, 3.25, 3.30, 3.35, 3.40))],
        # Watch-mode deal, F3 'locked' (premarket) and F5 'trap' (lucky buy).
        g("jb-55", "Jelly Bunny", 55, 1.50, "Pinky", "Ivory White", "Moon", t),
        g("jb-50", "Jelly Bunny", 50, 2.00, "Pinky", "Onyx Black", "Star", t),
        g("jb-51", "Jelly Bunny", 51, 2.05, "Pinky", "Mint Green", "Leaf", t),
        g("jb-52", "Jelly Bunny", 52, 2.10, "Pinky", "Sky Blue", "Pin", t),
        g("jb-53", "Jelly Bunny", 53, 1.20, "Pinky", "Coral Red", "Clip", t, isPremarket=True, unlockDate=unlock),
        g("jb-54", "Jelly Bunny", 54, 1.10, "Pinky", "Onyx Black", "Moon", t, luckyBuy=True),
        # F7 'cheaper elsewhere': 5.00 here, 4.60 all-in on Portals (fixture snapshot).
        g("sa-777", "Spy Agaric", 777, 5.00, "Amanita", "Onyx Black", "Star", t),
        *[g(f"sa-78{i}", "Spy Agaric", 780 + i, p, "Amanita", "Ivory White", "Moon", t)
          for i, p in enumerate((6.00, 6.10, 6.20, 6.30, 6.40))],
        # F8 'thin floor above Telegram': one comparable only; Telegram's average is lower.
        g("hc-1234", "Homemade Cake", 1234, 3.00, "Choco", "Sky Blue", "Pin", t),
        g("hc-1240", "Homemade Cake", 1240, 4.00, "Choco", "Mint Green", "Leaf", t),
    ]
    later = [
        # F4 'too good': 10% of the floor -> alert only, never auto-bought.
        g("ls-4416", "Lunar Snake", 4416, 0.16, "Albino", "Onyx Black", "Moon", "2026-10-28T10:01:00Z"),
        # F6 'healthy deal': ends in a practice buy.
        g("ls-4417", "Lunar Snake", 4417, 1.20, "Albino", "Mint Green", "Star", "2026-10-28T10:01:30Z"),
    ]
    return initial, later


DEMO_RULES = [
    BuyRule("Lunar Snake", 2.5, model="Albino", min_discount_pct=10, min_profit_ton=0.1, source="demo"),
    BuyRule("Lunar Snake", 0.99, model="Cobra", source="demo"),
    BuyRule("Desk Calendar", 2.5, min_discount_pct=15, mode="confirm", source="demo"),
    BuyRule("Jelly Bunny", 5.0, min_discount_pct=10, mode="watch", source="demo"),
    BuyRule("Spy Agaric", 5.5, source="demo"),
    BuyRule("Homemade Cake", 3.5, min_confidence="low", source="demo"),
]


class MockClient:
    """Offline stand-in with fixture data; call advance() between cycles to reveal new listings.

    scenario='basic' (the default) is a small 7-listing book the engine tests use; scenario='demo' is the
    richer book that `mock` runs (one listing per pricing case: trap, lock, thin floor, cheaper elsewhere...)."""

    name = "mock"

    def __init__(self, scenario: str = "basic", now: float | None = None) -> None:
        self.scenario = scenario
        if scenario == "demo":
            initial, later = demo_listings(time.time() if now is None else now)
            self._floors_raw = DEMO_FLOORS_RAW
        else:
            initial, later = MOCK_LISTINGS_INITIAL, MOCK_LISTINGS_LATER
            self._floors_raw = MOCK_FLOORS_RAW
        self._book: list[dict[str, Any]] = copy.deepcopy(initial)
        self._later: list[dict[str, Any]] = copy.deepcopy(later)
        self.bought: list[str] = []
        self.requests = 0
        self.fail_lookups = False
        self.last_server_date: float | None = None
        self.last_latency_ms: float | None = 120.0

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None

    def advance(self) -> None:
        """Reveal the next batch of fixture listings (demo: all at once, basic: one per call)."""
        if self.scenario == "demo":
            self._book.extend(self._later)
            self._later = []
        elif self._later:
            self._book.append(self._later.pop(0))

    def reprice(self, listing_id: str, price_ton: float) -> None:
        for raw in self._book:
            if raw["id"] == listing_id:
                raw["salePrice"] = ton_to_nano(price_ton)
                raw["salePriceWithoutFee"] = ton_to_nano(price_ton * 0.95)

    def remove(self, listing_id: str) -> None:
        self._book = [r for r in self._book if r["id"] != listing_id]

    def crash(self, collection: str, model: str | None, factor: float) -> None:
        """Move every listing of a collection/model by `factor` (mock --crash)."""
        for raw in self._book:
            if norm(raw["collectionName"]) == norm(collection) and (not model or norm(raw["modelName"]) == norm(model)):
                price = nano_to_ton(raw["salePrice"]) * factor
                raw["salePrice"] = ton_to_nano(round(price, 4))
                raw["salePriceWithoutFee"] = ton_to_nano(round(price * 0.95, 4))

    async def search_listings(self, filters: SearchFilters) -> list[Listing]:
        self.requests += 1

        def ok(raw: dict[str, Any]) -> bool:
            checks = (
                (filters.collections, raw.get("collectionName")),
                (filters.models, raw.get("modelName")),
                (filters.backdrops, raw.get("backdropName")),
                (filters.symbols, raw.get("symbolName")),
            )
            if filters.number is not None and raw.get("number") != filters.number:
                return False
            return all(not wanted or norm(value) in {norm(w) for w in wanted} for wanted, value in checks)

        items = [parse_listing(r) for r in self._book if ok(r)]
        if filters.ordering == ORDER_PRICE:
            items.sort(key=lambda l: l.price_ton, reverse=not filters.low_to_high)
        else:
            items.sort(key=lambda l: l.listed_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return items[: filters.count]

    async def collection_floors(self) -> dict[str, float]:
        self.requests += 1
        return parse_collection_floors(self._floors_raw)

    async def get_listing(self, listing_id: str, hint: Listing | None = None) -> Listing | None:
        self.requests += 1
        if self.fail_lookups:
            raise ApiError(0, "network error: mock lookup failure")
        raw = next((r for r in self._book if r["id"] == listing_id), None)
        return parse_listing(raw) if raw else None

    async def buy(self, listing: Listing) -> BuyResult:
        self._book = [r for r in self._book if r["id"] != listing.id]
        self.bought.append(listing.id)
        return BuyResult(True, "mock purchase", [{"userGift": listing.raw, "price": build_buy_payload(listing)}])

    async def balance(self) -> float:
        return 100.0
