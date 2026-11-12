"""Test ciclo check_deals con tutto mockato: DB sqlite memoria, scraper finti."""

import aiosqlite
import pytest
from types import SimpleNamespace
from deal_hunter.bot import app as bot


async def _make_db():
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, chat_id INTEGER, "
        "market_price REAL, max_price REAL, negative_keywords TEXT, is_active INTEGER DEFAULT 1)"
    )
    await db.execute(
        "CREATE TABLE found_deals (id INTEGER PRIMARY KEY, item_id INTEGER, url TEXT UNIQUE, "
        "price REAL, platform TEXT)"
    )
    await db.execute(
        "INSERT INTO items (name, chat_id, market_price, max_price, negative_keywords, is_active)"
        " VALUES ('Nintendo 3DS', 1, 100.0, NULL, 'custodia', 1)"
    )
    await db.commit()
    return db


class FakeVintedItem:
    def __init__(self, title, price, url):
        self.title = title
        self.price = price
        self.url = url


class FakeVScraper:
    async def search(self, params):
        return [
            FakeVintedItem("Nintendo 3DS XL funzionante", 60.0, "http://x/good"),
            FakeVintedItem("custodia per 3DS", 5.0, "http://x/case"),
        ]


class FakeVP:
    async def search_deals(self, *a, **k):
        raise AssertionError("fallback non dovrebbe servire in questo test")

    async def get_page_content(self, url):
        return None


class FakeCtx:
    def __init__(self):
        self.sent = []
        self.bot = self

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


async def test_check_single_item_notifies_only_good(monkeypatch):
    db = await _make_db()
    try:
        async with db.execute("SELECT * FROM items") as cur:
            item = await cur.fetchone()

        async def fake_fetch(url, playwright_scraper=None, http_client=None):
            if "good" in url:
                return {"base_price": 60.0, "shipping_price": 3.5,
                        "service_fee": 3.7, "total_price": 67.2}
            return {"base_price": 5.0, "shipping_price": 3.5,
                    "service_fee": 0.95, "total_price": 9.45}

        monkeypatch.setattr(bot, "fetch_item_details", fake_fetch)
        monkeypatch.setattr(bot, "notify_deal", _fake_notify := _recorder())
        # disattiva Jev: nessun TYPESAFE -> get_judge None
        from deal_hunter.jev import judge as deal_judge
        monkeypatch.setattr(deal_judge, "get_judge", lambda: None)
        import sys
        monkeypatch.setitem(sys.modules, "deal_hunter.jev.judge", deal_judge)

        ctx = FakeCtx()
        await bot._check_single_item(db, item, FakeVScraper(), FakeVP(), None, ctx)
        assert len(_fake_notify.calls) == 1
        assert "XL funzionante" in _fake_notify.calls[0]["title"]
    finally:
        await db.close()


def _recorder():
    async def _notify(db, chat_id, item_id, title, total_price, base_price,
                     shipping_price, service_fee, url, platform, market_price,
                     threshold, context, needs_review=False, **kwargs):
        _notify.calls.append({"title": title, "url": url, "total_price": total_price,
                              "needs_review": needs_review})
        await db.execute(
            "INSERT INTO found_deals (item_id, url, price, platform) VALUES (?, ?, ?, ?)",
            (item_id, url, total_price, platform),
        )
        await db.commit()

    _notify.calls = []
    return _notify


async def test_check_single_item_skips_duplicates(monkeypatch):
    db = await _make_db()
    try:
        await db.execute(
            "INSERT INTO found_deals (item_id, url, price, platform) VALUES (1, 'http://x/good', 67.2, 'Vinted')"
        )
        await db.commit()
        async with db.execute("SELECT * FROM items") as cur:
            item = await cur.fetchone()

        async def fake_fetch(url, playwright_scraper=None, http_client=None):
            return {"base_price": 60.0, "shipping_price": 3.5,
                    "service_fee": 3.7, "total_price": 67.2}

        monkeypatch.setattr(bot, "fetch_item_details", fake_fetch)
        calls = []
        monkeypatch.setattr(bot, "notify_deal", _rec2 := _recorder2(calls))
        from deal_hunter.jev import judge as deal_judge
        monkeypatch.setattr(deal_judge, "get_judge", lambda: None)
        import sys
        monkeypatch.setitem(sys.modules, "deal_hunter.jev.judge", deal_judge)

        await bot._check_single_item(db, item, FakeVScraper(), FakeVP(), None, FakeCtx())
        assert calls == []
    finally:
        await db.close()


def _recorder2(calls):
    async def _notify(*a, **k):
        calls.append(a)

    return _notify
