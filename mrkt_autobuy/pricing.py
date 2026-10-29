"""Honest price math: fees, robust floor with confidence, trap/lock flags and deal math.

Pure module: no I/O, the clock is always passed in. The engine, notifier, ledger and
wizard all build on it.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Iterable, Sequence

from .models import Listing

if TYPE_CHECKING:
    from .config import Settings

DAY = 86400.0
CONFIDENCE = ("low", "med", "high")


def conf_rank(level: str | None) -> int:
    return CONFIDENCE.index(level) if level in CONFIDENCE else 0


def downgrade(level: str) -> str:
    return CONFIDENCE[max(0, conf_rank(level) - 1)]


def signed_pct(value: float, digits: int = 0) -> str:
    """'+5%' / '−31%' with a real minus sign for user-facing text."""
    text = f"{abs(value):.{digits}f}%"
    return ("−" if value < 0 else "+") + text


def comparables(n: int, noun: str = "comparables") -> str:
    """'1 comparable' / '4 comparables' (also 'comparable listings')."""
    if n == 1:
        noun = noun.replace("comparables", "comparable").replace("listings", "listing")
    return f"{n} {noun}"


def signed_ton(value: float, digits: int = 2) -> str:
    return ("−" if value < 0 else "+") + f"{abs(value):.{digits}f}"


# --- fees ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FeeModel:
    """Percentages are in percent (5 = 5%); withdraw is a flat TON amount."""

    buy_pct: float = 0.0
    sell_pct: float = 0.0
    withdraw_ton: float = 0.0

    def cost(self, price: float) -> float:
        """All-in cost of buying at `price`."""
        return price * (1 + self.buy_pct / 100)

    def net_exit(self, price: float) -> float:
        """What reselling at `price` puts back in your pocket."""
        return price * (1 - self.sell_pct / 100) - self.withdraw_ton

    def break_even(self, floor: float) -> float:
        """Highest listed price you can pay and still break even when reselling at `floor`."""
        return (floor * (1 - self.sell_pct / 100) - self.withdraw_ton) / (1 + self.buy_pct / 100)

    def with_sell(self, sell_pct: float) -> "FeeModel":
        return replace(self, sell_pct=sell_pct)

    def describe(self, verified: bool) -> str:
        text = f"Fees: buy {self.buy_pct:g}% · sell {self.sell_pct:g}% · withdraw {self.withdraw_ton:g} TON"
        return text if verified else text + " (unverified)"


def fee_model_from_settings(s: "Settings") -> FeeModel:
    return FeeModel(s.BUY_FEE_PCT, s.SELL_FEE_PCT, s.WITHDRAW_FEE_TON)


def derive_sell_fee(raw: dict[str, Any]) -> float | None:
    """MRKT shows both salePrice and salePriceWithoutFee: the gap is the seller fee (percent)."""
    try:
        with_fee = float(raw.get("salePrice") or 0)
        without = float(raw.get("salePriceWithoutFee") or 0)
    except (TypeError, ValueError):
        return None
    if with_fee <= 0 or without <= 0 or without > with_fee:
        return None
    return round((1 - without / with_fee) * 100, 2)


# --- robust floor ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FloorEstimate:
    value: float | None
    source: str  # cluster | thin | api | none
    n: int
    confidence: str  # low | med | high
    note: str = ""

    def label(self) -> str:
        if self.value is None:
            return "no floor"
        return f"{self.value:.2f} ({self.source}, {comparables(self.n)}, {self.confidence})"


def cluster_floor(prices: Sequence[float], k: int = 2, band_pct: float = 10.0) -> float | None:
    """Lowest price that has at least `k` other asks within [p, p*(1+band)]."""
    ordered = sorted(prices)
    for i, p in enumerate(ordered):
        hi = p * (1 + band_pct / 100) + 1e-12
        others = sum(1 for q in ordered[i + 1 :] if q <= hi)
        if others >= k:
            return p
    return None


def cluster_size(prices: Sequence[float], floor: float, band_pct: float = 10.0) -> int:
    """How many asks (the floor itself included) sit within [floor, floor*(1+band)]."""
    hi = floor * (1 + band_pct / 100) + 1e-12
    return sum(1 for q in prices if floor - 1e-12 <= q <= hi)


# A floor needs a cluster of at least this many listings for 'med' or 'high' confidence, so two decoy
# listings can never produce a floor that passes the default min_confidence=med.
MIN_CLUSTER = 3


def floor_confidence(source: str, n: int, cluster_n: int, min_comparables: int) -> str:
    """high: a cluster of >= MIN_CLUSTER and >= min_comparables listings in all; med: a cluster of
    >= MIN_CLUSTER; low: anything else (a thin floor, the API floor, a 2-listing cluster, nothing)."""
    if source != "cluster" or cluster_n < MIN_CLUSTER:
        return "low"
    return "high" if n >= min_comparables else "med"


def floor_move(snapshots: Iterable[tuple[float, float]], now: float, window: float = 3600.0) -> float | None:
    """Largest relative move (percent, signed) of the newest snapshot versus older ones in the window."""
    recent = sorted((ts, v) for ts, v in snapshots if now - window <= ts <= now + 1 and v and v > 0)
    if len(recent) < 2:
        return None
    newest = recent[-1][1]
    best = 0.0
    for _, v in recent[:-1]:
        change = (newest - v) / v * 100
        if abs(change) > abs(best):
            best = change
    return best


def estimate_floor(
    listing: Listing | None,
    pool: Iterable[Listing],
    api_floor: float | None,
    recent_snapshots: Iterable[tuple[float, float]],
    now: float,
    *,
    k: int = 2,
    band: float = 10.0,
    min_comparables: int = 5,
    jump_pct: float = 25.0,
) -> FloorEstimate:
    """Floor that one troll ask (or one lone cheap ask) cannot define.

    `pool` is already per-rule comparable; the listing itself is excluded.
    """
    own_id = listing.id if listing is not None else None
    others = sorted(x.price_ton for x in pool if x.id != own_id and x.price_ton > 0)
    n = len(others)
    value = cluster_floor(others, k, band)
    if value is not None:
        source = "cluster"
    elif others:
        value = float(statistics.median(others[:3]))
        source = "thin"
    elif api_floor:
        value, source = float(api_floor), "api"
    else:
        value, source = None, "none"

    cluster_n = cluster_size(others, value, band) if source == "cluster" and value is not None else 0
    confidence = floor_confidence(source, n, cluster_n, min_comparables)

    note = ""
    move = floor_move(recent_snapshots, now)
    if move is not None and abs(move) > jump_pct:
        confidence = downgrade(confidence)
        note = f"floor {move:+.0f}% in 1h"
    return FloorEstimate(value, source, n, confidence, note)


# --- traps and locks ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Flag:
    code: str  # LOCKED | TRAP_LUCKY | TRAP_BUNDLE | BAD_PRICE | TOO_GOOD
    text: str
    blocking: bool
    days: float | None = None


# TODO(verify): MRKT unlock-date field names for premarket gifts; these are guesses.
UNLOCK_FIELDS = ("unlockDate", "unlockAt", "unlocksAt", "premarketUntil", "transferableAt", "canBeTransferredAt")
# TODO(verify): exact MRKT lucky-buy field names; any truthy lucky* field counts.
LUCKY_IGNORED = {"luckybuycashback"}
MRKT_PRICE_FIELD = "salePrice"  # nanoTON (verified against the web app bundle); client.parse_listing reads it


def _parse_ts(value: Any) -> float | None:
    if value in (None, "", 0):
        return None
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
    return None


def lock_days_from(raw: dict[str, Any], now: float) -> float | None:
    for key in UNLOCK_FIELDS:
        ts = _parse_ts(raw.get(key))
        if ts is not None:
            return max(0.0, math.ceil((ts - now) / DAY))
    return None


def classify(listing: Listing, market: str, now: float) -> list[Flag]:
    """Trap and lock flags. Fail closed: a missing or unknown price unit is blocking."""
    raw = listing.raw or {}
    flags: list[Flag] = []
    if listing.price_ton <= 0:
        flags.append(Flag("BAD_PRICE", "no price", True))
    currency = raw.get("currency") or raw.get("priceCurrency")
    if currency not in (None, "", "TON", "ton"):
        # TODO(verify): MRKT lists in TON (nanoTON); any other unit is refused until verified.
        flags.append(Flag("BAD_PRICE", f"unknown price unit {currency}", True))
    elif market == "mrkt" and not currency and raw.get(MRKT_PRICE_FIELD) is None:
        # The unit is known only from an explicit TON currency or MRKT's salePrice field (nanoTON, verified).
        flags.append(Flag("BAD_PRICE", f"price unit unknown (no {MRKT_PRICE_FIELD} in nanoTON)", True))
    if market == "mrkt":
        if raw.get("isPremarket"):
            days = lock_days_from(raw, now)
            text = f"premarket: unlocks in {days:.0f} d" if days is not None else "premarket: unlock date unknown"
            flags.append(Flag("LOCKED", text, False, days))
        for key, value in raw.items():
            if key.lower().startswith("lucky") and key.lower() not in LUCKY_IGNORED and value:
                flags.append(Flag("TRAP_LUCKY", "lucky-buy ticket, not a plain listing", True))
                break
    return flags


def too_good_flag(price: float, floor: float | None, pct: float = 25.0) -> Flag | None:
    if not floor or floor <= 0 or price <= 0:
        return None
    share = price / floor * 100
    if share < pct:
        return Flag("TOO_GOOD", f"price is {share:.0f}% of floor", False)
    return None


# --- deal math --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DealMath:
    price: float
    cost: float
    floor: float | None
    net_exit: float | None
    profit_ton: float | None
    profit_pct: float | None
    break_even_price: float | None
    discount_pct: float | None
    confidence: str
    flags: tuple[Flag, ...] = ()
    lock_days: float | None = None
    floor_source: str = "none"
    n: int = 0
    note: str = ""
    fees: FeeModel = field(default_factory=FeeModel)

    def has(self, code: str) -> bool:
        return any(f.code == code for f in self.flags)

    def flag(self, code: str) -> Flag | None:
        return next((f for f in self.flags if f.code == code), None)

    @property
    def blocking(self) -> list[Flag]:
        return [f for f in self.flags if f.blocking]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["flags"] = [asdict(f) for f in self.flags]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DealMath":
        data = dict(data)
        data["flags"] = tuple(Flag(**f) for f in data.get("flags", []))
        data["fees"] = FeeModel(**data.get("fees", {}))
        return cls(**data)


def assess(listing: Listing, floor_est: FloorEstimate, fees: FeeModel, flags: Iterable[Flag]) -> DealMath:
    price = listing.price_ton
    cost = fees.cost(price)
    floor = floor_est.value
    flags = tuple(flags)
    lock_days = None
    for f in flags:
        if f.code == "LOCKED":
            lock_days = f.days if f.days is not None else math.inf
    if floor and floor > 0:
        net_exit = fees.net_exit(floor)
        profit = net_exit - cost
        profit_pct = profit / cost * 100 if cost > 0 else None
        break_even = fees.break_even(floor)
        disc = (floor - price) / floor * 100
    else:
        net_exit = profit = profit_pct = break_even = disc = None
    return DealMath(
        price=price,
        cost=cost,
        floor=floor,
        net_exit=net_exit,
        profit_ton=profit,
        profit_pct=profit_pct,
        break_even_price=break_even,
        discount_pct=disc,
        confidence=floor_est.confidence,
        flags=flags,
        lock_days=lock_days,
        floor_source=floor_est.source,
        n=floor_est.n,
        note=floor_est.note,
        fees=fees,
    )


def legacy_deal(listing: Listing, floor: float | None) -> DealMath:
    """Fee-free DealMath around a bare floor number (scaffold-era callers and tests)."""
    est = FloorEstimate(floor, "given" if floor else "none", 0, "high")
    return assess(listing, est, FeeModel(), ())


def deal_summary(title: str, deal: DealMath) -> str:
    """One-line 'DEAL …' summary used in stdout cards and logs."""
    parts = [f"{title} {deal.price:.2f} TON"]
    if deal.floor is not None:
        parts.append(f"floor {deal.floor:.2f} ({deal.floor_source}, {comparables(deal.n)}, {deal.confidence})")
    parts.append(f"you pay {deal.cost:.2f} incl. {deal.fees.buy_pct:g}% fee")
    if deal.profit_ton is not None and deal.profit_pct is not None:
        parts.append(f"resold at floor: {deal.profit_ton:+.2f} TON net ({deal.profit_pct:+.0f}%)")
    if deal.break_even_price is not None:
        parts.append(f"break-even {deal.break_even_price:.2f}")
    return " · ".join(parts)
