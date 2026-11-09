from utils import (
    extract_dynamic_keywords,
    calculate_min_price,
    is_valid_item,
    parse_item_details_from_html,
    compute_service_fee,
    build_total_price,
    DEFAULT_SHIPPING,
)


def test_compute_service_fee():
    assert compute_service_fee(100) == 5.7
    assert compute_service_fee(0) == 0.7
    assert compute_service_fee(None) == 0.0


def test_build_total_price():
    assert build_total_price(100, 3.5, 5.7) == 109.2
    assert build_total_price(100) == round(100 + compute_service_fee(100) + DEFAULT_SHIPPING, 2)
    assert build_total_price(None) is None
    assert build_total_price("xx") is None


def test_calculate_min_price():
    assert calculate_min_price(100, 70) == 40.0
    assert calculate_min_price(None, 70) == 28.0
    assert calculate_min_price(None, None) == 0.0


def test_is_valid_item_base_keywords():
    assert not is_valid_item("Nintendo 3DS rotto", 50)
    assert not is_valid_item("3DS pezzi ricambio", 50)
    assert is_valid_item("Nintendo 3DS XL funzionante", 50)


def test_is_valid_item_min_price_and_dynamic():
    assert not is_valid_item("Nintendo 3DS", 5, min_price=40.0)
    assert not is_valid_item("Custodia per 3DS", 50, dynamic_keywords=["custodia"])
    assert is_valid_item("Nintendo 3DS", 50, dynamic_keywords=["custodia"])


def test_is_valid_item_market_calc_floor():
    assert not is_valid_item("Nintendo 3DS", 10, for_market_calc=True)
    assert is_valid_item("Nintendo 3DS", 20, for_market_calc=True)


def test_extract_dynamic_keywords_finds_accessory_words():
    items = (
        [{"title": "Nintendo 3DS XL funzionante", "price": 100}] * 8
        + [{"title": "custodia per 3DS", "price": 8}] * 4
    )
    kws = extract_dynamic_keywords(items, "Nintendo 3DS")
    assert "custodia" in kws


def test_extract_dynamic_keywords_empty():
    assert extract_dynamic_keywords([], "3DS") == []
    # senza bucket cheap -> nessuna keyword
    items = [{"title": "Nintendo 3DS", "price": 100}] * 5
    assert extract_dynamic_keywords(items, "Nintendo 3DS") == []


def test_parse_item_details_from_html_rsc_json():
    html = (
        '{"originalAskingAmount":{"amount":"70.00"},'
        '"shippingDetails":{"price":{"amount":"4.50"}},'
        '"buyerProtection":{"finalPrice":{"amount":"4.20"}}}'
    )
    out = parse_item_details_from_html(html)
    assert out["base_price"] == 70.0
    assert out["shipping_price"] == 4.5
    assert out["service_fee"] == 4.2
    assert out["total_price"] == round(70.0 + 4.2 + 4.5, 2)


def test_parse_item_details_fallback_fee():
    html = '{"originalAskingAmount":{"amount":"20.00"}}'
    out = parse_item_details_from_html(html)
    assert out["base_price"] == 20.0
    assert out["service_fee"] == compute_service_fee(20.0)
    assert out["total_price"] == round(20.0 + out["service_fee"] + 0.0, 2)
