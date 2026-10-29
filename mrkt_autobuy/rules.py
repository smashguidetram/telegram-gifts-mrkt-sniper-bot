"""Buy rules: definition, pure matching, parsing of /addrule and rules.json."""

from __future__ import annotations

import json
import math
import shlex
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .models import Listing, norm
from .pricing import CONFIDENCE, DealMath, comparables, conf_rank, legacy_deal

MODES = ("auto", "confirm", "watch")  # strictness grows left to right
EXITS = ("here", "best")
STATES = ("shadow", "live", "paused")

# Reason codes for match(); human text lives next to them in MatchResult.reason.
OK = "OK"
REASON_CODES = (
    "DISABLED",
    "MISMATCH",
    "BAD_PRICE",
    "TRAP_BUNDLE",
    "TRAP_LUCKY",
    "LOCKED",
    "OVER_MAX",
    "LOW_CONFIDENCE",
    "LOW_DISCOUNT",
    "LOSES_AFTER_FEES",
    "LOW_PROFIT",
)


class RuleParseError(ValueError):
    pass


@dataclass(slots=True)
class BuyRule:
    collection: str
    max_price_ton: float
    model: str | None = None
    backdrop: str | None = None
    symbol: str | None = None
    min_discount_pct: float = 0.0
    id: int | None = None
    enabled: bool = True
    source: str = "bot"  # bot | file | demo
    min_profit_ton: float = 0.0
    min_profit_pct: float = 0.0
    min_confidence: str = "med"  # low | med | high
    allow_locked_days: float = 0.0  # 0 = never buy locked gifts
    mode: str = "auto"  # auto | confirm | watch
    exit: str = "here"  # here | best
    state: str = "live"  # shadow | live | paused (shadow = practice-only even in LIVE)
    promoted_at: float | None = None

    def filter_key(self) -> tuple[str, str, str, str]:
        """Rules with the same key share one API search per cycle."""
        return (norm(self.collection), norm(self.model), norm(self.backdrop), norm(self.symbol))

    def describe(self) -> str:
        parts = [f"#{self.id}" if self.id is not None else "#new", self.collection, f"max={self.max_price_ton:g}"]
        for key in ("model", "backdrop", "symbol"):
            value = getattr(self, key)
            if value:
                parts.append(f"{key}={value}")
        if self.min_discount_pct:
            parts.append(f"discount={self.min_discount_pct:g}%")
        if self.min_profit_ton:
            parts.append(f"profit={self.min_profit_ton:g}")
        if self.min_profit_pct:
            parts.append(f"profit%={self.min_profit_pct:g}")
        if self.min_confidence != "med":
            parts.append(f"conf={self.min_confidence}")
        if self.allow_locked_days:
            parts.append(f"locked={self.allow_locked_days:g}")
        if self.mode != "auto":
            parts.append(f"mode={self.mode}")
        if self.exit != "here":
            parts.append(f"exit={self.exit}")
        if self.state != "live":
            parts.append(f"[{self.state}]")
        if not self.enabled:
            parts.append("(disabled)")
        return " ".join(parts)

    def filter_key_str(self) -> str:
        return "|".join(self.filter_key())

    @property
    def pins_attributes(self) -> bool:
        return bool(self.model or self.backdrop or self.symbol)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True, frozen=True)
class MatchResult:
    ok: bool
    reason: str
    discount_pct: float | None = None
    code: str = OK
    deal: DealMath | None = None


def discount_pct(price_ton: float, floor_ton: float | None) -> float | None:
    """How far below the floor the price is, in percent (negative = above floor)."""
    if floor_ton is None or floor_ton <= 0:
        return None
    return (floor_ton - price_ton) / floor_ton * 100.0


def stricter_mode(*modes: str) -> str:
    return max(modes, key=lambda m: MODES.index(m) if m in MODES else 0)


def match(rule: BuyRule, listing: Listing, floor_ton: "float | DealMath | None" = None) -> MatchResult:
    """Pure decision. Order: attributes, blocking flags, lock, max price on cost, confidence,
    discount, profit. `floor_ton` is a DealMath (net math) or, for old callers, a bare floor."""
    deal = floor_ton if isinstance(floor_ton, DealMath) else legacy_deal(listing, floor_ton)
    disc = deal.discount_pct

    def no(code: str, text: str) -> MatchResult:
        return MatchResult(False, text, disc, code, deal)

    if not rule.enabled:
        return no("DISABLED", "rule disabled")
    if norm(rule.collection) != norm(listing.collection):
        return no("MISMATCH", "collection mismatch")
    for attr in ("model", "backdrop", "symbol"):
        wanted = getattr(rule, attr)
        if wanted and norm(wanted) != norm(getattr(listing, attr)):
            return no("MISMATCH", f"{attr} mismatch")
    if listing.price_ton <= 0:
        return no("BAD_PRICE", "invalid price")
    for flag in deal.blocking:
        code = flag.code if flag.code in REASON_CODES else "BAD_PRICE"
        return no(code, flag.text)
    if deal.has("LOCKED"):
        days = deal.lock_days
        if rule.allow_locked_days <= 0 or days is None or days > rule.allow_locked_days:
            flag = deal.flag("LOCKED")
            return no("LOCKED", f"locked: {flag.text if flag else 'unlock date unknown'}")
    if deal.cost > rule.max_price_ton + 1e-9:
        if abs(deal.cost - deal.price) < 1e-9:
            return no("OVER_MAX", f"price {deal.price:g} > max {rule.max_price_ton:g}")
        return no("OVER_MAX", f"cost {deal.cost:.2f} incl. fees > max {rule.max_price_ton:g}")
    if conf_rank(deal.confidence) < conf_rank(rule.min_confidence):
        return no("LOW_CONFIDENCE", f"floor confidence {deal.confidence} < {rule.min_confidence} ({comparables(deal.n)})")
    if rule.min_discount_pct > 0:
        if disc is None:
            return no("LOW_DISCOUNT", "no floor to compare with")
        if disc < rule.min_discount_pct:
            return no("LOW_DISCOUNT", f"discount {disc:.1f}% < {rule.min_discount_pct:g}%")
    if deal.profit_ton is None:
        if rule.min_profit_ton > 0 or rule.min_profit_pct > 0:
            return no("LOW_PROFIT", "no floor to estimate profit")
    elif deal.profit_ton < 0:
        return no("LOSES_AFTER_FEES", f"loses {abs(deal.profit_ton):.2f} TON after fees")
    elif deal.profit_ton < rule.min_profit_ton:
        return no("LOW_PROFIT", f"profit {deal.profit_ton:+.2f} TON < {rule.min_profit_ton:g} TON")
    elif rule.min_profit_pct > 0 and (deal.profit_pct or 0) < rule.min_profit_pct:
        return no("LOW_PROFIT", f"profit {deal.profit_pct or 0:+.1f}% < {rule.min_profit_pct:g}%")
    return MatchResult(True, "match", disc, OK, deal)


@dataclass(slots=True, frozen=True)
class NearMiss:
    check: str  # max | discount | profit
    value: float
    limit: float
    miss_pct: float
    new_max: float | None = None

    def text(self) -> str:
        if self.check == "max":
            return f"Missed by a hair: {self.value:.2f} TON, your max is {self.limit:.2f}"
        if self.check == "discount":
            return f"Missed by a hair: discount {self.value:.1f}%, your minimum is {self.limit:g}%"
        return f"Missed by a hair: profit {self.value:+.2f} TON, your minimum is {self.limit:g} TON"


def near_miss(rule: BuyRule, listing: Listing, deal: DealMath, *, pct: float = 5.0) -> NearMiss | None:
    """A listing that fails exactly one numeric check (max on cost, discount or profit) by <= pct."""
    relaxed = replace(rule, max_price_ton=math.inf, min_discount_pct=0.0, min_profit_ton=0.0, min_profit_pct=0.0)
    if not match(relaxed, listing, deal).ok:
        return None
    misses: list[NearMiss] = []
    if rule.max_price_ton <= 0:  # an old stored row with max 0 must not crash the cycle
        return None
    if deal.cost > rule.max_price_ton + 1e-9:
        new_max = math.ceil(round(deal.cost / 0.05, 6)) * 0.05
        misses.append(NearMiss("max", deal.cost, rule.max_price_ton,
                               (deal.cost - rule.max_price_ton) / rule.max_price_ton * 100, round(new_max, 2)))
    if rule.min_discount_pct > 0 and deal.discount_pct is not None and deal.discount_pct < rule.min_discount_pct:
        misses.append(NearMiss("discount", deal.discount_pct, rule.min_discount_pct,
                               (rule.min_discount_pct - deal.discount_pct) / rule.min_discount_pct * 100))
    if rule.min_profit_ton > 0 and deal.profit_ton is not None and deal.profit_ton < rule.min_profit_ton:
        misses.append(NearMiss("profit", deal.profit_ton, rule.min_profit_ton,
                               (rule.min_profit_ton - deal.profit_ton) / rule.min_profit_ton * 100))
    if len(misses) != 1 or misses[0].miss_pct > pct:
        return None
    return misses[0]


# --- /addrule parsing -------------------------------------------------------

_KEY_ALIASES = {
    "max": "max_price_ton",
    "max_price": "max_price_ton",
    "max_price_ton": "max_price_ton",
    "model": "model",
    "backdrop": "backdrop",
    "bg": "backdrop",
    "symbol": "symbol",
    "discount": "min_discount_pct",
    "min_discount": "min_discount_pct",
    "min_discount_pct": "min_discount_pct",
    "profit": "min_profit_ton",
    "min_profit": "min_profit_ton",
    "min_profit_ton": "min_profit_ton",
    "profit%": "min_profit_pct",
    "min_profit_pct": "min_profit_pct",
    "conf": "min_confidence",
    "confidence": "min_confidence",
    "min_confidence": "min_confidence",
    "locked": "allow_locked_days",
    "allow_locked_days": "allow_locked_days",
    "mode": "mode",
    "exit": "exit",
}


def _choice(key: str, value: str, allowed: tuple[str, ...]) -> str:
    value = value.strip().lower()
    if value not in allowed:
        raise RuleParseError(f"{key} must be one of {'|'.join(allowed)}, got {value!r}")
    return value


def _to_float(key: str, value: str) -> float:
    try:
        number = float(value.replace(",", ".").rstrip("%"))
    except ValueError as exc:
        raise RuleParseError(f"{key} must be a number, got {value!r}") from exc
    if not math.isfinite(number):  # nan or inf would silently switch a limit off
        raise RuleParseError(f"{key} must be a finite number, got {value!r}")
    return number


def parse_addrule_args(text: str) -> BuyRule:
    """Parse `<collection> max=<ton> [model=..] [backdrop=..] [symbol=..] [discount=<pct>]
    [profit=<ton>] [profit%=<pct>] [conf=low|med|high] [locked=<days>] [mode=auto|confirm|watch] [exit=here|best]`.

    Multi-word names work unquoted ("Lunar Snake max=3 model=Pink Panther") or quoted.
    """
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise RuleParseError(f"cannot parse arguments: {exc}") from exc

    collection_parts: list[str] = []
    values: dict[str, list[str]] = {}
    current: str | None = None
    for tok in tokens:
        key, sep, value = tok.partition("=")
        if sep and key.lower() in _KEY_ALIASES:
            current = _KEY_ALIASES[key.lower()]
            values[current] = [value] if value else []
        elif current is None:
            collection_parts.append(tok)
        else:
            values[current].append(tok)

    collection = " ".join(collection_parts).strip()
    if not collection:
        raise RuleParseError("collection is required")
    if "max_price_ton" not in values or not values["max_price_ton"]:
        raise RuleParseError("max=<ton> is required")

    max_price = _to_float("max", " ".join(values["max_price_ton"]))
    if max_price <= 0:
        raise RuleParseError("max must be > 0")
    discount = _to_float("discount", " ".join(values.get("min_discount_pct", ["0"])) or "0")
    if not 0 <= discount < 100:
        raise RuleParseError("discount must be in [0, 100)")

    def opt(key: str) -> str | None:
        joined = " ".join(values.get(key, [])).strip()
        return joined or None

    def num(key: str, label: str) -> float:
        value = _to_float(label, opt(key) or "0")
        if value < 0:
            raise RuleParseError(f"{label} must be >= 0")
        return value

    return BuyRule(
        collection=collection,
        max_price_ton=max_price,
        model=opt("model"),
        backdrop=opt("backdrop"),
        symbol=opt("symbol"),
        min_discount_pct=discount,
        min_profit_ton=num("min_profit_ton", "profit"),
        min_profit_pct=num("min_profit_pct", "profit%"),
        min_confidence=_choice("conf", opt("min_confidence") or "med", CONFIDENCE),
        allow_locked_days=num("allow_locked_days", "locked"),
        mode=_choice("mode", opt("mode") or "auto", MODES),
        exit=_choice("exit", opt("exit") or "here", EXITS),
    )


# --- rules.json ---------------------------------------------------------------


STATE_UNSET = ""  # a rules.json entry without "state"; Storage.replace_rules_from_source resolves it


def _file_number(data: dict[str, Any], *keys: str) -> float:
    """A non-negative, finite number from a rules.json entry (the first key present wins; missing = 0)."""
    raw = next((data[k] for k in keys if data.get(k) is not None), 0)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RuleParseError(f"{keys[0]} must be a number, got {raw!r}") from exc
    if not math.isfinite(value) or value < 0:
        raise RuleParseError(f"{keys[0]} must be a finite number >= 0, got {raw!r}")
    return value


def rule_from_dict(data: dict[str, Any], source: str = "file") -> BuyRule:
    if not isinstance(data, dict):
        raise RuleParseError(f"rule must be an object, got {type(data).__name__}")
    collection = data.get("collection")
    if not collection or data.get("max_price_ton", data.get("max")) is None:
        raise RuleParseError(f"rule needs 'collection' and 'max_price_ton': {data}")
    max_price = _file_number(data, "max_price_ton", "max")
    if max_price <= 0:  # same rule as /addrule; use "enabled": false to switch a rule off
        raise RuleParseError(f"rule for {collection!r}: max_price_ton must be > 0 (got {max_price:g}); "
                             'use "enabled": false to switch it off')
    return BuyRule(
        collection=str(collection),
        max_price_ton=max_price,
        model=data.get("model") or None,
        backdrop=data.get("backdrop") or None,
        symbol=data.get("symbol") or None,
        min_discount_pct=_file_number(data, "min_discount_pct", "discount"),
        enabled=bool(data.get("enabled", True)),
        source=source,
        min_profit_ton=_file_number(data, "min_profit_ton"),
        min_profit_pct=_file_number(data, "min_profit_pct"),
        min_confidence=_choice("min_confidence", str(data.get("min_confidence") or "med"), CONFIDENCE),
        allow_locked_days=_file_number(data, "allow_locked_days"),
        mode=_choice("mode", str(data.get("mode") or "auto"), MODES),
        exit=_choice("exit", str(data.get("exit") or "here"), EXITS),
        # No "state": STATE_UNSET. The loader starts a new rule in shadow (SHADOW_NEW_RULES) and keeps the
        # stored state (a promotion, a pause) of an existing one; only an explicit "state" overwrites it.
        state=_choice("state", str(data["state"]), STATES) if data.get("state") else STATE_UNSET,
    )


def file_rule_keys(rules: list[BuyRule]) -> list[str]:
    """Stable identity of each rules.json entry: its filter key, plus '#2', '#3'... for a repeated filter.
    Changing max, discount, mode etc. keeps the rule's id; changing collection/model/backdrop/symbol makes a new rule."""
    seen: dict[str, int] = {}
    keys = []
    for rule in rules:
        base = rule.filter_key_str()
        seen[base] = seen.get(base, 0) + 1
        keys.append(base if seen[base] == 1 else f"{base}#{seen[base]}")
    return keys


def load_rules_file(path: str | Path) -> list[BuyRule]:
    """Load rules from a JSON list. Missing file -> empty list."""
    p = Path(path)
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("rules", [])
    if not isinstance(data, list):
        raise RuleParseError(f"{p}: expected a JSON list of rules")
    return [rule_from_dict(item) for item in data]
