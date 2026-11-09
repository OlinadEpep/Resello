"""Test parsing Playwright senza rete: mock get_page_content con HTML fixture."""

import pytest
from vinted_playwright import VintedPlaywrightScraper

HTML = """
<html><body>
<div data-testid="grid-item-1">
  <span data-testid="title-x">Nintendo 3DS XL funzionante</span>
  <span data-testid="price-x">70,00 €</span>
  <a href="/items/111-abc">link</a>
</div>
<div data-testid="grid-item-2">
  <span data-testid="title-x">custodia per 3DS</span>
  <span data-testid="price-x">5,00 €</span>
  <a href="/items/222-def">link</a>
</div>
<div data-testid="grid-item-3">
  <span data-testid="title-x">Nintendo 3DS rotto pezzi</span>
  <span data-testid="price-x">20,00 €</span>
  <a href="/items/333-ghi">link</a>
</div>
</body></html>
"""


async def test_search_deals_respects_threshold_and_keywords(monkeypatch):
    vp = VintedPlaywrightScraper()

    async def fake_content(url):
        return HTML

    monkeypatch.setattr(vp, "get_page_content", fake_content)
    deals = await vp.search_deals("Nintendo 3DS", 80, dynamic_keywords=["custodia"])
    titles = [d["title"] for d in deals]
    assert "Nintendo 3DS XL funzionante" in titles
    assert "custodia per 3DS" not in titles  # filtrata da dynamic keywords
    assert "Nintendo 3DS rotto pezzi" not in titles  # filtrata da base keywords


async def test_search_deals_respects_min_price(monkeypatch):
    vp = VintedPlaywrightScraper()

    async def fake_content(url):
        return HTML

    monkeypatch.setattr(vp, "get_page_content", fake_content)
    deals = await vp.search_deals("Nintendo 3DS", 200, min_price=50.0, dynamic_keywords=[])
    prices = [d["price"] for d in deals]
    assert prices and all(p >= 50.0 for p in prices)


async def test_get_page_content_alias(monkeypatch):
    vp = VintedPlaywrightScraper()

    async def fake_content(url):
        return "<html></html>"

    monkeypatch.setattr(vp, "get_page_content", fake_content)
    assert await vp._get_page_content("http://x") == "<html></html>"
