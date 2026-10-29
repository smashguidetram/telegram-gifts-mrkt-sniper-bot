import json

import pytest

from mrkt_autobuy.models import Listing, norm
from mrkt_autobuy.rules import (
    BuyRule,
    RuleParseError,
    discount_pct,
    load_rules_file,
    match,
    parse_addrule_args,
)


def L(price, collection="Lunar Snake", model="Albino", backdrop="Onyx Black", symbol="Star", lid="g1"):
    return Listing(id=lid, collection=collection, model=model, backdrop=backdrop, symbol=symbol, number=1,
                   price_ton=price)


def test_norm_ignores_case_and_spaces():
    assert norm("Lunar Snake") == norm("lunarsnake") == norm("Lunar-Snake")
    assert norm(None) == ""


def test_discount_pct():
    assert discount_pct(1.0, 2.0) == pytest.approx(50.0)
    assert discount_pct(3.0, 2.0) == pytest.approx(-50.0)
    assert discount_pct(1.0, None) is None
    assert discount_pct(1.0, 0) is None


def test_match_accepts_cheap_listing():
    rule = BuyRule(collection="lunar snake", max_price_ton=2.5, min_discount_pct=20)
    res = match(rule, L(1.2), floor_ton=3.0)
    assert res.ok and res.discount_pct == pytest.approx(60.0)


@pytest.mark.parametrize(
    "rule,listing,floor,reason",
    [
        (BuyRule("Desk Calendar", 5), L(1.0), 2.0, "collection mismatch"),
        (BuyRule("Lunar Snake", 5, model="Cobra"), L(1.0), 2.0, "model mismatch"),
        (BuyRule("Lunar Snake", 5, backdrop="Black"), L(1.0), 2.0, "backdrop mismatch"),
        (BuyRule("Lunar Snake", 5, symbol="Moon"), L(1.0), 2.0, "symbol mismatch"),
        (BuyRule("Lunar Snake", 2.5), L(2.95), 3.0, "price 2.95 > max 2.5"),
        (BuyRule("Lunar Snake", 5, min_discount_pct=20), L(2.8), 3.0, "discount"),
        (BuyRule("Lunar Snake", 5, min_discount_pct=20), L(1.0), None, "no floor"),
        (BuyRule("Lunar Snake", 5, enabled=False), L(1.0), 2.0, "disabled"),
    ],
)
def test_match_rejects(rule, listing, floor, reason):
    res = match(rule, listing, floor)
    assert not res.ok
    assert reason in res.reason


def test_match_without_discount_needs_no_floor():
    assert match(BuyRule("Lunar Snake", 2.0), L(1.5), None).ok


def test_match_attribute_filters_are_case_insensitive():
    rule = BuyRule("Lunar Snake", 2.0, model="albino", backdrop="onyx black", symbol="STAR")
    assert match(rule, L(1.5), None).ok


def test_parse_addrule_multiword():
    rule = parse_addrule_args("Lunar Snake max=2.5 model=Pink Panther discount=20")
    assert rule.collection == "Lunar Snake"
    assert rule.max_price_ton == 2.5
    assert rule.model == "Pink Panther"
    assert rule.min_discount_pct == 20
    assert rule.backdrop is None and rule.symbol is None


def test_parse_addrule_quoted_and_comma_decimal():
    rule = parse_addrule_args('"Plush Pepe" max=1,5 backdrop="Onyx Black" symbol=Star discount=15%')
    assert (rule.collection, rule.max_price_ton, rule.backdrop, rule.symbol, rule.min_discount_pct) == (
        "Plush Pepe", 1.5, "Onyx Black", "Star", 15.0)


@pytest.mark.parametrize("text", ["", "max=2", "Lunar Snake", "Lunar Snake max=abc", "Lunar Snake max=0",
                                  "Lunar Snake max=1 discount=150"])
def test_parse_addrule_errors(text):
    with pytest.raises(RuleParseError):
        parse_addrule_args(text)


def test_load_rules_file(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps([
        {"collection": "Lunar Snake", "max_price_ton": 2.5, "min_discount_pct": 20},
        {"collection": "Desk Calendar", "max": 1.5, "model": "Classic", "discount": 10},
    ]))
    rules = load_rules_file(p)
    assert [r.collection for r in rules] == ["Lunar Snake", "Desk Calendar"]
    assert rules[1].max_price_ton == 1.5 and rules[1].model == "Classic" and rules[1].min_discount_pct == 10
    assert all(r.source == "file" for r in rules)
    assert load_rules_file(tmp_path / "missing.json") == []


def test_load_rules_file_invalid(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text(json.dumps([{"collection": "X"}]))
    with pytest.raises(RuleParseError):
        load_rules_file(p)
