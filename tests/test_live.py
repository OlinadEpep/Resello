"""Test live opt-in: skippati di default. Esegui con LIVE=1."""

import os
import pytest

pytestmark = pytest.mark.live


def _live_enabled():
    return os.getenv("LIVE") == "1"


@pytest.mark.skipif(os.getenv("LIVE") != "1", reason="live opt-in: LIVE=1 per eseguire")
async def test_jev_live_accessory_vs_product():
    from deal_judge import DealJudge
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        pytest.skip("TYPESAFE_API_KEY mancante")
    j = DealJudge(api_key=api_key, db_path=":memory:")
    good = await j.judge("Nintendo 3DS", "Nintendo 3DS XL blu funzionante con caricabatterie",
                         65.0, market_price=110.0, threshold=77.0, url="live-good")
    case = await j.judge("Nintendo 3DS", "custodia rigida per Nintendo 3DS",
                         9.0, market_price=110.0, threshold=77.0, url="live-case")
    assert good["decision"] in ("notify", "review")
    assert case["decision"] in ("skip", "review")


@pytest.mark.skipif(os.getenv("LIVE") != "1", reason="live opt-in: LIVE=1 per eseguire")
async def test_vinted_library_live():
    from vinted_scraper import AsyncVintedScraper
    scraper = await AsyncVintedScraper.create("https://www.vinted.it")
    async with scraper:
        items = await scraper.search({"search_text": "Nintendo 3DS"})
    assert isinstance(items, list)
