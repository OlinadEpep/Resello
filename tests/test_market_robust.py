"""Fase 4: regression PS5 + guardrail + sanity + Jev-mock."""

from deal_hunter.vinted import market as vinted_market
from deal_hunter.core.utils import robust_median, sanity_check_market, extract_dynamic_keywords


class FakeItem:
    _n = 0

    def __init__(self, title, price):
        self.title = title
        self.price = price
        FakeItem._n += 1
        self.url = f"https://vinted.it/items/fake-{FakeItem._n}"


class FakeScraper:
    _next_items = []

    @classmethod
    async def create(cls, *a, **k):
        return cls()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def search(self, params):
        # Solo pagina 1 con dati, pagina 2 vuota (evita duplicati mock)
        if params.get("page", 1) > 1:
            return []
        return list(self._next_items)


def _patch(monkeypatch, items, jev_map=None):
    FakeScraper._next_items = items
    monkeypatch.setattr(vinted_market, "AsyncVintedScraper", FakeScraper)

    async def no_fallback(query):
        return None, [], {'reason': 'mocked'}
    monkeypatch.setattr(vinted_market, "_get_vinted_market_price_fallback", no_fallback)

    if jev_map is None:
        async def no_jev(query, items_data):
            return items_data, {'n_raw': len(items_data), 'n_kept': len(items_data),
                                'reason': 'mocked_no_jev'}
        monkeypatch.setattr(vinted_market, "_apply_jev_market_filter", no_jev)
    else:
        async def mock_jev(query, items_data):
            kept = [d for d in items_data if jev_map(d['title'])]
            return kept, {'n_raw': len(items_data), 'n_kept': len(kept),
                          'reason': 'jev_filtered'}
        monkeypatch.setattr(vinted_market, "_apply_jev_market_filter", mock_jev)


def _ps5_mix():
    consoles = [
        ("PlayStation 5 console disco perfetta", 320),
        ("PS5 slim lettore disco", 340),
        ("Playstation 5 digital edition", 280),
        ("PS5 + 2 controller", 380),
        ("Sony Playstation 5 console", 300),
        ("Playstation 5 console + giochi", 330),
    ]
    junk = [
        ("DualSense controller bianco PS5", 45),
        ("Controller PS5 joystick nero", 40),
        ("Stazione ricarica PS5 controller", 18),
        ("Cover PS5 coveroli", 15),
        ("Supporto verticale PS5", 12),
        ("Gioco PS5 Spiderman 2", 28),
        ("Gioco playstation 5 FC25", 25),
        ("Cuffie headset per PS5", 22),
        ("Cavo HDMI PS5", 8),
        ("Scatola vuota PS5", 10),
        ("Skin adesiva PS5", 7),
        ("Porta giochi PS5 custodia", 6),
    ]
    return [FakeItem(t, p) for t, p in consoles + junk]


async def test_ps5_regex_only_rejects_or_flags(monkeypatch):
    """Solo regex: o scarta (None+meta) o resta sopra 200€, mai 166€."""
    _patch(monkeypatch, _ps5_mix())
    price, kws, meta = await vinted_market.get_vinted_market_price("playstation 5")
    # Con solo regex la distribuzione è bimodale -> guardrail IQR deve scattare
    # oppure la mediana dei soli full resta alta. In nessun caso 100-200€ sporca.
    if price is not None:
        assert price > 200, f"mediana inquinata: {price} meta={meta}"
    else:
        assert meta.get('robust_reason') or meta.get('reason')


async def test_ps5_with_jev_mock_gives_console_price(monkeypatch):
    """Con filtro Jev (mock: tiene solo titoli console) prezzo ~300-400€."""
    def is_console(title):
        t = title.lower()
        return 'console' in t or 'slim' in t or 'digital edition' in t or 'ps5 + 2' in t
    _patch(monkeypatch, _ps5_mix(), jev_map=is_console)
    price, kws, meta = await vinted_market.get_vinted_market_price("PlayStation 5")
    assert price is not None and 250 < price < 450, f"{price} {meta}"
    assert meta.get('n_full', 0) >= 5


async def test_robust_median_rejects_bimodal():
    prices = [20.0] * 10 + [350.0] * 6
    price, meta = robust_median(prices)
    assert price is None and 'dispersion' in meta


async def test_robust_median_ok_on_clean():
    prices = [300 + i for i in range(8)]
    price, meta = robust_median(prices)
    assert price is not None and 295 < price < 315


async def test_sanity_ps5_band():
    ok, _, sugg = sanity_check_market("playstation 5", 166.0)
    assert not ok and sugg
    ok2, _, _ = sanity_check_market("playstation 5", 330.0)
    assert ok2
    ok3, reason3, _ = sanity_check_market("death stranding 2 ps5", 40.95)
    assert ok3 and reason3 == 'game_query_skipped'


async def test_dynamic_finds_box_now():
    from deal_hunter.core.utils import compute_service_fee, DEFAULT_SHIPPING
    items = ([{'title': 'PS5 console disco', 'price': 330.0}] * 8
             + [{'title': 'scatola vuota PS5', 'price': 15.0}] * 4)
    kws = extract_dynamic_keywords(items, 'playstation 5')
    assert 'scatola' in kws, f"scatola deve emergere ora, kws={kws}"
