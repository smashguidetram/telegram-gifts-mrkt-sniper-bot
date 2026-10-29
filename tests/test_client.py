import pytest

from mrkt_autobuy.client import (
    ORDER_NEWEST,
    ORDER_PRICE,
    SearchFilters,
    build_buy_payload,
    build_saling_payload,
    parse_balance,
    parse_buy_response,
    parse_collection_floors,
    parse_listing,
    parse_listings,
)
from mrkt_autobuy.rules import BuyRule
from mrkt_autobuy.tg_auth import extract_tg_web_app_data, mask

RAW_GIFT = {
    "id": "0f6d2c1e-aaaa-bbbb-cccc-1234567890ab",
    "collectionName": "Lunar Snake",
    "collectionTitle": "Lunar Snake",
    "number": 1042,
    "modelName": "Albino",
    "backdropName": "Onyx Black",
    "symbolName": "Star",
    "salePrice": 1_234_500_000,
    "salePriceWithoutFee": 1_180_000_000,
    "isOnSale": True,
}


def test_parse_listing_converts_nanoton():
    l = parse_listing(RAW_GIFT)
    assert l.id == RAW_GIFT["id"]
    assert (l.collection, l.model, l.backdrop, l.symbol, l.number) == ("Lunar Snake", "Albino", "Onyx Black", "Star", 1042)
    assert l.price_ton == pytest.approx(1.2345)
    assert l.key.endswith(":1234500000")
    assert "#1042" in l.title


def test_parse_listings_skips_garbage():
    out = parse_listings({"gifts": [RAW_GIFT, {"no": "id"}], "cursor": "x"})
    assert len(out) == 1
    assert parse_listings([RAW_GIFT])[0].id == RAW_GIFT["id"]
    assert parse_listings(None) == []


def test_saling_payload_for_rule():
    rule = BuyRule("Lunar Snake", 2.5, model="Albino")
    cheap = build_saling_payload(SearchFilters.for_rule(rule, count=20))
    assert cheap["collectionNames"] == ["Lunar Snake"]
    assert cheap["modelNames"] == ["Albino"]
    assert cheap["backdropNames"] == [] and cheap["symbolNames"] == []
    assert cheap["ordering"] == ORDER_PRICE and cheap["lowToHigh"] is True
    assert cheap["count"] == 20 and cheap["cursor"] == ""
    assert cheap["removeSelfSales"] is True
    newest = build_saling_payload(SearchFilters.for_rule(rule, newest=True))
    assert newest["ordering"] == ORDER_NEWEST and newest["lowToHigh"] is False


def test_saling_payload_price_bounds_in_nanoton():
    p = build_saling_payload(SearchFilters(min_price_ton=0.5, max_price_ton=2))
    assert p["minPrice"] == 500_000_000 and p["maxPrice"] == 2_000_000_000


def test_buy_payload_matches_web_app():
    l = parse_listing(RAW_GIFT)
    assert build_buy_payload(l) == {"ids": [RAW_GIFT["id"]], "prices": {RAW_GIFT["id"]: 1_234_500_000}}


def test_parse_collection_floors():
    data = [
        {"name": "LunarSnake", "title": "Lunar Snake", "floorPriceNanoTons": 3_000_000_000},
        {"name": "Empty", "title": "Empty", "floorPriceNanoTons": 0},
    ]
    floors = parse_collection_floors(data)
    assert floors == {"LunarSnake": 3.0, "Lunar Snake": 3.0}


def test_parse_balance_and_buy_response():
    assert parse_balance({"hard": 12_500_000_000, "soft": 1}) == pytest.approx(12.5)
    assert parse_buy_response([{"userGift": {}, "price": 1}], "x").ok
    assert not parse_buy_response([], "x").ok
    assert not parse_buy_response({"error": "NOT_FOR_SALE"}, "x").ok


def test_extract_tg_web_app_data_from_fragment():
    init = "query_id=AAH&user=%7B%22id%22%3A1%7D&auth_date=1700000000&hash=abc123"
    from urllib.parse import quote

    url = f"https://cdn.tgmrkt.io/#tgWebAppData={quote(init, safe='')}&tgWebAppVersion=8.0&tgWebAppPlatform=android"
    assert extract_tg_web_app_data(url) == init


def test_extract_tg_web_app_data_missing():
    with pytest.raises(ValueError):
        extract_tg_web_app_data("https://cdn.tgmrkt.io/#tgWebAppVersion=8.0")


def test_mask_never_returns_full_secret():
    secret = "query_id=AAH&user=...&hash=deadbeef"
    assert secret not in mask(secret)
    assert mask("") == "<empty>"
