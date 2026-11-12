"""Test offline del controllo periodico di /notifica: DB sqlite temporaneo, bot finto."""

import aiosqlite
import pytest

import init_db
import notifica


class FakeBot:
    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("telegram giù")
        self.sent.append((chat_id, text))


class FakeScraper:
    """Restituisce pagine predefinite; il parsing è sostituito da `fake_parse`."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get_page_content(self, url):
        self.calls.append(url)
        return self.pages.get(url)


def item(item_id, title=None):
    return {
        "id": item_id, "title": title or f"Annuncio {item_id}", "brand": "adidas",
        "size": "M", "condition": "Ottime", "price": 20.0, "total_price": 21.7,
        "url": f"https://www.vinted.it/items/{item_id}-x",
    }


URL = "https://www.vinted.it/catalog?catalog[]=260&brand_ids[]=14&order=newest_first"
MARGIN = 1000


@pytest.fixture
async def db(tmp_path, monkeypatch):
    path = str(tmp_path / "test.db")
    await init_db.init_db(path)
    monkeypatch.setattr(notifica, "DB_PATH", path)
    monkeypatch.setattr(notifica, "ALERT_ID_MARGIN", MARGIN)
    monkeypatch.setattr(notifica, "ALERT_MAX_NOTIFY", 3)
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    yield conn
    await conn.close()


async def _alert(db, chat_id=1, url=URL, label="Pantaloni · adidas", match_text=None):
    status, alert_id = await notifica.create_alert(db, chat_id, label, url, match_text=match_text)
    assert status == "created"
    return alert_id


async def _row(db, alert_id):
    (row,) = await notifica._active_alerts(db, alert_id)
    return row


async def _no_global():
    raise AssertionError("il watermark globale non doveva servire")


async def _step(db, alert_id, items, bot, global_wm=_no_global):
    return await notifica.process_alert(
        db, await _row(db, alert_id), items, bot, global_wm, pause=0
    )


async def test_first_pass_only_records_existing_listings(db):
    bot, alert_id = FakeBot(), await _alert(db)
    assert await _step(db, alert_id, [item(5000), item(4990)], bot) == 0
    assert bot.sent == []
    row = await _row(db, alert_id)
    assert row["seeded"] == 1 and row["watermark"] == 5000


async def test_new_listing_is_notified_once_with_both_links(db):
    bot, alert_id = FakeBot(), await _alert(db, chat_id=42)
    await _step(db, alert_id, [item(5000), item(4990)], bot)

    page = [item(5100, "Jeans <nuovi> & belli"), item(5000), item(4990)]
    assert await _step(db, alert_id, page, bot) == 1
    (chat_id, text), = bot.sent
    assert chat_id == 42
    assert "Jeans &lt;nuovi&gt; &amp; belli" in text          # HTML sicuro
    assert 'href="https://www.vinted.it/items/5100-x"' in text  # link annuncio
    assert "catalog[]=260&amp;brand_ids[]=14&amp;order=newest_first" in text  # link ricerca filtrata
    assert "Pantaloni · adidas" in text and "20.00€" in text and "Taglia M" in text

    # stesso contenuto al giro dopo: nessun doppione
    assert await _step(db, alert_id, page, bot) == 0
    assert len(bot.sent) == 1
    assert (await _row(db, alert_id))["watermark"] == 5100


async def test_old_listings_resurfacing_are_not_notified(db):
    bot, alert_id = FakeBot(), await _alert(db)
    await _step(db, alert_id, [item(50_000), item(49_990)], bot)
    # 49_990 venduto: risale un annuncio vecchio (pagina 2) + uno rimesso in evidenza
    assert await _step(db, alert_id, [item(50_000), item(30_000), item(12_345)], bot) == 0
    assert bot.sent == []


async def test_burst_is_capped_and_summarised(db):
    bot, alert_id = FakeBot(), await _alert(db)
    await _step(db, alert_id, [item(5000)], bot)
    page = [item(5000 + i) for i in range(5, 0, -1)] + [item(5000)]
    assert await _step(db, alert_id, page, bot) == 3      # ALERT_MAX_NOTIFY
    texts = [t for _, t in bot.sent]
    assert ["Annuncio 5003" in texts[0], "Annuncio 5004" in texts[1], "Annuncio 5005" in texts[2]] == [True] * 3
    assert "Altri <b>2</b> nuovi annunci" in texts[3]
    # quelli non inviati singolarmente non tornano al giro dopo
    assert await _step(db, alert_id, page, bot) == 0
    assert len(bot.sent) == 4


async def test_failed_read_changes_nothing(db):
    bot, alert_id = FakeBot(), await _alert(db)
    assert await _step(db, alert_id, None, bot) == 0
    assert (await _row(db, alert_id))["seeded"] == 0
    await _step(db, alert_id, [item(5000)], bot)
    assert await _step(db, alert_id, None, bot) == 0
    assert (await _row(db, alert_id))["watermark"] == 5000


async def test_empty_search_starts_from_newest_listing_on_vinted(db):
    bot, alert_id = FakeBot(), await _alert(db)

    async def global_wm():
        return 90_000

    assert await _step(db, alert_id, [], bot, global_wm) == 0
    row = await _row(db, alert_id)
    assert row["seeded"] == 1 and row["watermark"] == 90_000
    # compare un annuncio vecchio (non nuovo) e uno davvero nuovo
    assert await _step(db, alert_id, [item(90_500), item(40_000)], bot) == 1
    assert "Annuncio 90500" in bot.sent[0][1]


async def test_empty_search_without_reference_point_retries_later(db):
    bot, alert_id = FakeBot(), await _alert(db)

    async def global_wm():
        return 0  # nemmeno la pagina generale è leggibile

    assert await _step(db, alert_id, [], bot, global_wm) == 0
    assert (await _row(db, alert_id))["seeded"] == 0


async def test_send_failure_is_retried_on_next_cycle(db):
    bot, alert_id = FakeBot(fail=True), await _alert(db)
    await _step(db, alert_id, [item(5000)], bot)
    assert await _step(db, alert_id, [item(5100), item(5000)], bot) == 0
    bot.fail = False
    assert await _step(db, alert_id, [item(5100), item(5000)], bot) == 1
    assert await _step(db, alert_id, [item(5100), item(5000)], bot) == 0
    assert len(bot.sent) == 1


async def test_seen_ids_below_threshold_are_pruned(db):
    bot, alert_id = FakeBot(), await _alert(db)
    await _step(db, alert_id, [item(5000), item(100)], bot)
    await _step(db, alert_id, [item(9000), item(5000), item(100)], bot)
    async with db.execute("SELECT item_id FROM alert_seen WHERE alert_id = ? ORDER BY item_id", (alert_id,)) as cur:
        assert [r[0] for r in await cur.fetchall()] == [9000]  # soglia = 9000 - 1000


async def test_cycle_reads_each_search_once_and_notifies_every_chat(db, monkeypatch):
    a1, a2 = await _alert(db, chat_id=1), await _alert(db, chat_id=2)
    pages = {URL: "pagina-1"}
    listing = {"pagina-1": [item(5000)], "pagina-2": [item(5200), item(5000)]}
    monkeypatch.setattr(notifica.vf, "parse_catalog_items", lambda html, base_url=None: listing[html])
    scraper, bot = FakeScraper(pages), FakeBot()

    alerts = await notifica._active_alerts(db)
    assert await notifica.run_alert_cycle(db, alerts, scraper, bot, pause=0) == 0   # prima lettura
    pages[URL] = "pagina-2"
    alerts = await notifica._active_alerts(db)
    assert await notifica.run_alert_cycle(db, alerts, scraper, bot, pause=0) == 2
    assert sorted(chat for chat, _ in bot.sent) == [1, 2]
    assert scraper.calls == [URL, URL]  # 2 giri, una lettura per giro nonostante 2 chat


async def test_cycle_survives_blocked_page(db):
    alert_id = await _alert(db)
    scraper, bot = FakeScraper({}), FakeBot()   # get_page_content -> None (es. Cloudflare)
    alerts = await notifica._active_alerts(db)
    assert await notifica.run_alert_cycle(db, alerts, scraper, bot, pause=0) == 0
    assert (await _row(db, alert_id))["seeded"] == 0


async def test_create_alert_duplicate_limit_and_delete(db, monkeypatch):
    alert_id = await _alert(db, chat_id=7)
    assert await notifica.create_alert(db, 7, "x", URL) == ("duplicate", None)
    status, other = await notifica.create_alert(db, 8, "x", URL)   # altra chat: ok
    assert status == "created"

    monkeypatch.setattr(notifica, "MAX_ALERTS_PER_CHAT", 1)
    assert await notifica.create_alert(db, 7, "y", URL + "&price_to=10") == ("limit", None)

    assert await notifica.delete_alert(db, alert_id, chat_id=8) is None   # non è sua
    assert await notifica.delete_alert(db, alert_id, chat_id=7) == "Pantaloni · adidas"
    assert [a["id"] for a in await notifica._active_alerts(db)] == [other]


def test_alert_message_without_optional_fields():
    text = notifica.format_alert_message("Etichetta", URL, {
        "id": 1, "title": "Solo titolo", "brand": None, "size": None, "condition": None,
        "price": None, "total_price": None, "url": "https://www.vinted.it/items/1-x",
    })
    assert "Solo titolo" in text and "🏷" not in text and "💰" not in text


# --- nome esatto di un prodotto ---------------------------------------------

PRODUCT_URL = "https://www.vinted.it/catalog?search_text=rtx%205060&order=newest_first"


async def test_product_alert_notifies_only_titles_with_the_exact_name(db):
    bot = FakeBot()
    alert_id = await _alert(db, url=PRODUCT_URL, label="Prodotto «rtx 5060»", match_text="rtx 5060")
    assert (await _row(db, alert_id))["match_text"] == "rtx 5060"
    await _step(db, alert_id, [item(5000, "Rtx 5060 ventus")], bot)

    page = [
        item(5040, "MSI RTX5060TI 16GB"),                 # sì: scritto attaccato
        item(5030, "RTX 3070 Ti Founders Edition"),       # no: Vinted lo mostra perché simile
        item(5020, "Gaming PC (vedi descrizione)"),       # no: il nome è solo in descrizione
        item(5010, "Scheda video rtx 5060 8gb"),          # sì
        item(5000, "Rtx 5060 ventus"),
    ]
    assert await _step(db, alert_id, page, bot) == 2
    texts = [t for _, t in bot.sent]
    assert "Scheda video rtx 5060 8gb" in texts[0] and "MSI RTX5060TI 16GB" in texts[1]
    assert all("Prodotto «rtx 5060»" in t and "search_text=rtx%205060" in t for t in texts)

    # gli scartati sono comunque "già visti": non tornano e non vengono ricontati
    assert await _step(db, alert_id, page, bot) == 0
    assert len(bot.sent) == 2
    assert (await _row(db, alert_id))["watermark"] == 5040


async def test_product_alert_matches_brand_and_caps_only_real_matches(db):
    bot = FakeBot()
    alert_id = await _alert(db, url=PRODUCT_URL, label="Prodotto «logitech mx vertical»",
                            match_text="logitech mx vertical")
    await _step(db, alert_id, [item(5000)], bot)
    page = [dict(item(5010, "Mouse MX Vertical"), brand="Logitech")]
    page += [item(5020 + i, "Mouse Trust qualsiasi") for i in range(10)]   # 10 non pertinenti
    assert await _step(db, alert_id, page, bot) == 1
    assert len(bot.sent) == 1 and "Mouse MX Vertical" in bot.sent[0][1]    # nessun "Altri N annunci"


async def test_same_search_url_with_and_without_exact_name(db):
    """Stesso link in due chat: una col nome esatto, una senza (link incollato)."""
    bot = FakeBot()
    exact = await _alert(db, chat_id=1, url=PRODUCT_URL, label="Prodotto «rtx 5060»", match_text="rtx 5060")
    loose = await _alert(db, chat_id=2, url=PRODUCT_URL, label="«rtx 5060»")
    for alert_id in (exact, loose):
        await _step(db, alert_id, [item(5000)], bot)
    page = [item(5020, "RTX 3070 Ti"), item(5010, "Rtx 5060 nuova"), item(5000)]
    assert await _step(db, exact, page, bot) == 1
    assert await _step(db, loose, page, bot) == 2
    assert [chat for chat, _ in bot.sent] == [1, 2, 2]


async def test_init_db_adds_match_text_to_existing_alerts_table(tmp_path):
    path = str(tmp_path / "old.db")
    async with aiosqlite.connect(path) as conn:      # tabella com'era prima della colonna
        await conn.execute(
            "CREATE TABLE alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
            "label TEXT NOT NULL, search_url TEXT NOT NULL, watermark INTEGER DEFAULT 0, "
            "seeded INTEGER DEFAULT 0, is_active INTEGER DEFAULT 1, "
            "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, last_check DATETIME, "
            "UNIQUE(chat_id, search_url))"
        )
        await conn.execute("INSERT INTO alerts (chat_id, label, search_url) VALUES (1, 'vecchia', ?)", (URL,))
        await conn.commit()
    await init_db.init_db(path)
    await init_db.init_db(path)                       # idempotente
    async with aiosqlite.connect(path) as conn:
        (old,) = await notifica._active_alerts(conn)
        assert old["label"] == "vecchia" and old["match_text"] is None
        assert await notifica.create_alert(conn, 1, "p", PRODUCT_URL, match_text="rtx 5060") == ("created", 2)
