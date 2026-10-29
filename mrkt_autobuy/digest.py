"""Daily digest: one silent message with yesterday's numbers, top skip reasons and tuning hints.

Optional 'Other bots' section: when PEER_EVENT_FILES lists other bots' events.jsonl files, their counts are
added (missing, unreadable or schema-mismatched files are skipped with a single warning).
"""

from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any, Iterable

from .events import read_events, validate_event
from .explain import reason_text
from .ledger import summarize

if TYPE_CHECKING:
    from .engine import Engine

log = logging.getLogger(__name__)
_warned: set[str] = set()

TUNABLE = {"OVER_MAX": "max price", "LOW_DISCOUNT": "min discount", "LOW_PROFIT": "min profit",
           "LIMIT_MAX_PRICE": "MAX_PRICE_TON", "LIMIT_DAILY_BUDGET": "DAILY_BUDGET_TON", "LIMIT_HOURLY": "MAX_BUYS_PER_HOUR"}


def aggregate_events(events: Iterable[dict[str, Any]], since: float, until: float) -> dict[str, Any]:
    """Counts per kind, buys by mode, money in USD where present, and top skip reasons."""
    kinds: Counter = Counter()
    reasons: Counter = Counter()
    modes: Counter = Counter()
    usd = 0.0
    for e in events:
        ts = e.get("ts")
        if not isinstance(ts, (int, float)) or not since <= ts < until:
            continue
        kind = e.get("kind")
        kinds[kind] += 1
        if kind == "buy":
            modes[e.get("mode")] += 1
            money = e.get("money") or {}
            if isinstance(money.get("usd"), (int, float)):
                usd += money["usd"]
        if kind == "skip":
            for r in e.get("reasons") or []:
                if isinstance(r, dict) and r.get("code"):
                    reasons[r["code"]] += 1
    return {"alerts": kinds["alert"], "buys": kinds["buy"], "skips": kinds["skip"], "errors": kinds["error"],
            "buys_by_mode": dict(modes), "usd": round(usd, 2), "top_reasons": reasons.most_common(3)}


def load_peer_events(paths: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Other bots' events.jsonl files. Bad files are skipped with one warning each, never a crash."""
    out: dict[str, list[dict[str, Any]]] = {}
    for path in paths:
        try:
            events = read_events(path)
            if not events:
                raise FileNotFoundError(path)
            if validate_event(events[-1]):
                raise ValueError("schema mismatch")
            out[path] = events
        except Exception as exc:
            if path not in _warned:
                _warned.add(path)
                log.warning("peer events %s skipped: %s", path, type(exc).__name__)
    return out


async def build_digest(engine: "Engine", since: float, until: float, *, title: str = "Daily digest") -> str:
    storage, s = engine.storage, engine.settings
    observed = [o for o in await storage.observations_since(since) if o["first_seen"] < until]
    decisions = await storage.decisions_since(since, until)
    alerts = sum(1 for d in decisions if d.outcome == "alert")
    buys = [d for d in decisions if d.outcome in ("dry", "bought")]
    positions = await storage.positions(since=since)
    positions = [p for p in positions if p.created_at < until]
    days = (until - since) / 86400 + 1
    practice = summarize(positions, practice=True, days=days, now=until, fees=engine.fees)["total"]
    live = summarize(positions, practice=False, days=days, now=until, fees=engine.fees)["total"]
    reasons = Counter({k: v for k, v in (await storage.reason_counts(since, until)).items()})
    lines = [
        f"{title}:",
        f"Listings scanned: {len({o['listing_id'] for o in observed})} · alerts: {alerts} · buys: {len(buys)}",
        f"Practice net: {practice.net:+.2f} TON ({practice.buys} buys) · Live net: {live.net:+.2f} TON ({live.buys} buys)",
    ]
    top = reasons.most_common(3)
    if top:
        lines.append("Top skip reasons: " + " · ".join(f"{code} {n} ({reason_text(code)})" for code, n in top))
        for code, n in top:
            if code not in TUNABLE:
                continue
            winners = [d for d in decisions if d.reason_code == code and (d.detail.get("profit_ton") or 0) > 0]
            if winners:
                gain = sum(d.detail["profit_ton"] for d in winners)
                skipped = "1 deal was" if n == 1 else f"{n} deals were"
                plus = "it was" if n == 1 else (f"{len(winners)} of them was" if len(winners) == 1
                                               else f"{len(winners)} of them were")
                lines.append(f"Tuning hint: {skipped} skipped by {code} ({TUNABLE[code]}), and {plus} "
                             f"+net at the floor ({gain:+.2f} TON). Near-miss cards offer a one-tap raise.")
                break
    else:
        lines.append("Top skip reasons: none")
    day = int((until - 1 + s.TZ_OFFSET_HOURS * 3600) // 86400)
    errors = await storage.get_setting(f"errors:{day}", "0")
    lines.append(f"API errors: {errors}")
    peers = load_peer_events(s.peer_event_files)
    if peers:
        lines.append("Other bots:")
        for path, events in peers.items():
            agg = aggregate_events(events, since, until)
            bot = next((e.get("bot") for e in events if e.get("bot")), path)
            lines.append(f"  {bot}: {agg['alerts']} alerts · {agg['buys']} buys · {agg['skips']} skips"
                         + (f" · ${agg['usd']:.2f} bought" if agg["usd"] else ""))
    return "\n".join(lines)
