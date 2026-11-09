"""Test Idea 3: throttle mercato + refresh. Idea review-banner."""

import aiosqlite
import pytest
import bot


async def _db_with_item(last_update_sql="CURRENT_TIMESTAMP"):
    db = await aiosqlite.connect(":memory:")
    db.row_factory = aiosqlite.Row
    await db.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, chat_id INTEGER,"
        " market_price REAL, max_price REAL, negative_keywords TEXT,"
        " last_market_update DATETIME, is_active INTEGER DEFAULT 1)"
    )
    await db.execute(
        "CREATE TABLE found_deals (id INTEGER PRIMARY KEY, item_id INTEGER, url TEXT UNIQUE,"
        " price REAL, platform TEXT)"
    )
    await db.execute(
        f"INSERT INTO items (name, chat_id, market_price, max_price, negative_keywords,"
        f" last_market_update, is_active) VALUES ('Nintendo 3DS', 1, 100.0, NULL, 'custodia',"
        f" {last_update_sql}, 1)"
    )
    await db.commit()
    return db


async def test_maybe_refresh_skips_fresh():
    db = await _db_with_item("CURRENT_TIMESTAMP")
    try:
        calls = []

        async def fake_market(q):
            calls.append(q)
            return 999.0, []

        import bot as _b
        _orig = _b.get_vinted_market_price
        _b.get_vinted_market_price = fake_market
        try:
            async with db.execute("SELECT * FROM items") as cur:
                items = await cur.fetchall()
            out = await _b._maybe_refresh_market(db, items)
            assert out == {} and calls == []
        finally:
            _b.get_vinted_market_price = _orig
    finally:
        await db.close()


async def test_maybe_refresh_updates_stale():
    db = await _db_with_item("datetime('now', '-10 hours')")
    try:
        async def fake_market(q):
            return 111.0, ["pluto"]

        import bot as _b
        _orig = _b.get_vinted_market_price
        _b.get_vinted_market_price = fake_market
        try:
            async with db.execute("SELECT * FROM items") as cur:
                items = await cur.fetchall()
            out = await _b._maybe_refresh_market(db, items)
            assert 1 in out and out[1][0] == 111.0
            async with db.execute("SELECT market_price FROM items WHERE id=1") as cur:
                row = await cur.fetchone()
            assert row[0] == 111.0
        finally:
            _b.get_vinted_market_price = _orig
    finally:
        await db.close()


async def test_maybe_refresh_fail_open():
    db = await _db_with_item("datetime('now', '-10 hours')")
    try:
        async def boom(q):
            raise RuntimeError("vinted down")

        import bot as _b
        _orig = _b.get_vinted_market_price
        _b.get_vinted_market_price = boom
        try:
            async with db.execute("SELECT * FROM items") as cur:
                items = await cur.fetchall()
            out = await _b._maybe_refresh_market(db, items)
            assert out == {}
            async with db.execute("SELECT market_price FROM items WHERE id=1") as cur:
                row = await cur.fetchone()
            assert row[0] == 100.0  # vecchio prezzo conservato
        finally:
            _b.get_vinted_market_price = _orig
    finally:
        await db.close()


async def test_notify_deal_review_banner():
    db = await aiosqlite.connect(":memory:")
    try:
        await db.execute(
            "CREATE TABLE found_deals (id INTEGER PRIMARY KEY, item_id INTEGER, url TEXT UNIQUE,"
            " price REAL, platform TEXT)"
        )
        await db.commit()

        class Ctx:
            def __init__(self):
                self.sent = []
                self.bot = self

            async def send_message(self, chat_id, text, parse_mode=None):
                self.sent.append(text)

        ctx = Ctx()
        await bot.notify_deal(db, 1, 1, "Nintendo 3DS", 60.0, 55.0, 3.5, 3.7,
                              "http://x/r1", "Vinted", 100.0, 70.0, ctx, needs_review=True)
        assert "Da verificare" in ctx.sent[0]
        ctx2 = Ctx()
        await bot.notify_deal(db, 1, 1, "Altro", 60.0, 55.0, 3.5, 3.7,
                              "http://x/r2", "Vinted", 100.0, 70.0, ctx2)
        assert "Da verificare" not in ctx2.sent[0]
    finally:
        await db.close()
