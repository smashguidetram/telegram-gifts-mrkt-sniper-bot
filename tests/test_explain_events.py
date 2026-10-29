import asyncio
import re
from pathlib import Path

from helpers import NOW, L, demo_engine, settings

from mrkt_autobuy import explain
from mrkt_autobuy.config import Settings
from mrkt_autobuy.digest import aggregate_events, build_digest, load_peer_events
from mrkt_autobuy.events import EventLog, read_events, validate_event
from mrkt_autobuy.notifier import DealView, render_deal_card
from mrkt_autobuy.pricing import FeeModel, FloorEstimate, assess
from mrkt_autobuy.rules import REASON_CODES, BuyRule

PKG = Path(__file__).resolve().parents[1] / "mrkt_autobuy"
FIXTURE = Path(__file__).parent / "fixtures" / "events_sample.jsonl"


def _codes_in_source() -> set[str]:
    """Reason codes as they appear in the engine: report.reasons[...], _decide(...), downgrades, limits, taps."""
    pattern = re.compile(r'"([A-Z][A-Z]+(?:_[A-Z]+)+|[A-Z]{4,})"')
    settings_keys = set(Settings.model_fields)
    found: set[str] = set()
    for name in ("engine.py", "peers.py", "pricing.py", "rules.py"):
        for code in pattern.findall((PKG / name).read_text()):
            if code not in settings_keys and code not in {"TON", "USD", "USDC", "JPY", "KRW", "VND", "CLP", "ISK",
                                                          "UGX", "XAF", "XOF", "BHD", "KWD", "OMR", "JOD", "TND",
                                                          "IQD", "LYD", "MRKT", "HTML", "STATUS", "SKIP", "WATCH",
                                                          "AVOID", "BUY", "COPY", "TRAP"}:
                found.add(code)
    return found


def test_every_reason_code_has_an_english_template():
    codes = set(REASON_CODES) | set(explain.SUITE_CODE) | set(explain.SUITE_CODE.values()) | _codes_in_source()
    missing = sorted(c for c in codes if c not in explain.REASON_TEXT)
    assert missing == []
    assert all(isinstance(t, str) and t and t.isascii() for t in explain.REASON_TEXT.values())


def test_validator_rejects_missing_v_bot_kind():
    good = {"v": 1, "ts": NOW, "bot": "mrkt-autobuy", "kind": "skip", "mode": "dry", "id": "x",
            "subject": {"type": "gift", "ref": "x"}, "reasons": []}
    assert validate_event(good) == []
    for key in ("v", "bot", "kind"):
        bad = dict(good)
        bad.pop(key)
        assert any(key in e for e in validate_event(bad))
    assert validate_event({**good, "kind": "dance"})
    assert validate_event("nope")


def test_rotation_at_events_max_mb(tmp_path):
    log = EventLog(tmp_path / "events.jsonl", max_mb=0.001)  # ~1 KB
    for i in range(30):
        log.append({"v": 1, "ts": NOW + i, "bot": "mrkt-autobuy", "kind": "skip", "mode": "dry", "id": str(i),
                    "subject": {"type": "gift", "ref": str(i)}, "reasons": []})
    assert (tmp_path / "events.jsonl.1").exists()
    assert (tmp_path / "events.jsonl").stat().st_size <= 1100


def test_rendered_alert_is_short_and_escaped():
    listing = L("x", 1.0, collection="<script>" + "A" * 300, number=1)
    deal = assess(listing, FloorEstimate(1.6, "cluster", 4, "high"), FeeModel(0, 5, 0), [])
    card = render_deal_card(DealView(listing, deal, BuyRule(listing.collection, 3, id=1), "confirm", "Waiting",
                                     True, token="tok12345", pending=True))
    assert len(card.html) < 4096 and "<script>" not in card.html and "&lt;script&gt;" in card.html


def test_llm_path_makes_no_call_without_key():
    async def boom(*args):
        raise AssertionError("network call attempted")

    s = settings(ANTHROPIC_API_KEY="")
    assert asyncio.run(explain.explain_more({"x": 1}, s, call=boom)) is None
    assert not explain.llm_available(s)

    seen = []

    async def fake(key, model, prompt):
        seen.append((key, model, prompt))
        return "Plain words."

    keyed = settings(ANTHROPIC_API_KEY="sk-secret-123")
    assert asyncio.run(explain.explain_more({"title": "Lunar Snake #1"}, keyed, call=fake)) == "Plain words."
    key, model, prompt = seen[0]
    assert model == "claude-sonnet-5-5" and "sk-secret-123" not in prompt and "Lunar Snake #1" in prompt


def test_digest_aggregation_matches_fixture_events():
    events = read_events(FIXTURE)
    assert len(events) == 7  # the garbage line is skipped
    agg = aggregate_events(events, 1800000000, 1800086400)
    assert (agg["alerts"], agg["buys"], agg["skips"]) == (1, 2, 3)
    assert agg["buys_by_mode"] == {"dry": 1, "live": 1}
    assert agg["usd"] == 6.25
    assert agg["top_reasons"] == [("PRICE_ABOVE_MAX", 2), ("FEES_EAT_PROFIT", 1)]


def test_peer_event_files_missing_or_malformed_are_skipped(tmp_path, caplog):
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"hello": 1}\n')
    out = load_peer_events([str(tmp_path / "missing.jsonl"), str(bad), str(FIXTURE)])
    assert list(out) == [str(FIXTURE)]


def test_engine_writes_valid_events_and_digest_has_top_reasons(tmp_path):
    async def go():
        log = EventLog(tmp_path / "events.jsonl")
        engine, storage, client, notifier, clock = await demo_engine(events=log)
        try:
            await engine.run_cycle()
            events = log.read()
            assert events and all(validate_event(e) == [] for e in events)
            kinds = {e["kind"] for e in events}
            assert {"alert", "buy", "skip"} <= kinds
            f2 = [e for e in events if e["subject"]["ref"] == "ls-4412"]
            assert f2[0]["reasons"][0]["code"] == "FEES_EAT_PROFIT"
            text = await build_digest(engine, clock() - 86400, clock() + 1)
            assert "Top skip reasons: OVER_MAX" in text
        finally:
            await storage.close()

    asyncio.run(go())
