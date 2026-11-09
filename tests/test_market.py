import types
import pytest
import vinted_market


class FakeItem:
    _n = 0

    def __init__(self, title, price, service_fee=None):
        self.title = title
        self.price = price
        FakeItem._n += 1
        self.url = f"https://vinted.it/items/fake-{FakeItem._n}"
        if service_fee is not None:
            self.service_fee = service_fee


class FakeScraper:
    def __init__(self, items):
        self._items = items

    @classmethod
    async def create(cls, *a, **k):
        return cls(cls._next_items)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def search(self, params):
        return self._items


def _patch(monkeypatch, items):
    FakeScraper._next_items = items
    monkeypatch.setattr(vinted_market, "AsyncVintedScraper", FakeScraper)
    # evita fallback Playwright reale nei test
    async def no_fallback(query):
        return None, [], {'reason': 'mocked'}
    monkeypatch.setattr(vinted_market, "_get_vinted_market_price_fallback", no_fallback)
    # evita Jev reale: fail-open neutro (tiene tutto)
    async def no_jev(query, items_data):
        return items_data, {'n_raw': len(items_data), 'n_kept': len(items_data),
                            'reason': 'mocked_no_jev'}
    monkeypatch.setattr(vinted_market, "_apply_jev_market_filter", no_jev)


async def _get3(query):
    res = await vinted_market.get_vinted_market_price(query)
    assert len(res) == 3
    return res


async def test_market_price_median_trimmed(monkeypatch):
    items = [FakeItem(f"Nintendo 3DS console {i}", 90 + i) for i in range(10)]
    _patch(monkeypatch, items)
    price, kws, meta = await _get3("Nintendo 3DS")
    assert price is not None and 90 < price < 110
    assert isinstance(kws, list)


async def test_market_price_no_items_returns_fallback(monkeypatch):
    _patch(monkeypatch, [])
    price, kws, meta = await _get3("Nintendo 3DS")
    assert price is None and kws == []


async def test_market_price_filters_accessories(monkeypatch):
    good = [FakeItem("Nintendo 3DS XL funzionante", 100) for _ in range(8)]
    cheap = [FakeItem("custodia per 3DS", 8) for _ in range(4)]
    _patch(monkeypatch, good + cheap)
    price, kws, meta = await _get3("Nintendo 3DS")
    assert price is not None and price > 50  # le custodie non trascinano la mediana
