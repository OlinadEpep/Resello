"""Conversazione /notifica end-to-end dentro una vera Application PTB, senza rete.

Le richieste a Telegram finiscono in `FakeRequest` (che le registra) e le
chiamate a Vinted sono sostituite: così si verifica il cablaggio reale di
handler, gruppi, stati e pattern dei pulsanti.
"""

import asyncio
import json

import aiosqlite
import pytest
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler
from telegram.request import BaseRequest

from deal_hunter.core import db as init_db
from deal_hunter.bot import notifica

CHAT_ID, USER_ID = 555, 777
CATS = [
    {"id": 1817, "title": "Jeans skinny", "path": ["Uomo", "Vestiti", "Jeans", "Jeans skinny"]},
    {"id": 257, "title": "Jeans", "path": ["Uomo", "Vestiti", "Jeans"]},
]


class FakeRequest(BaseRequest):
    def __init__(self):
        self.calls = []
        self._msg_id = 1000

    read_timeout = None

    async def initialize(self):
        pass

    async def shutdown(self):
        pass

    async def do_request(self, url, method, request_data=None, *args, **kwargs):
        api_method = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((api_method, params))
        if api_method == "getMe":
            result = {"id": 1, "is_bot": True, "first_name": "Deal", "username": "deal_test_bot"}
        elif api_method in ("sendMessage", "editMessageText", "editMessageReplyMarkup"):
            self._msg_id += 1
            result = {"message_id": self._msg_id, "date": 0,
                      "chat": {"id": CHAT_ID, "type": "private"}, "text": "x"}
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()

    def texts(self):
        return [p.get("text", "") for m, p in self.calls if m in ("sendMessage", "editMessageText")]

    def last_keyboard(self):
        for _, p in reversed(self.calls):
            if p.get("reply_markup"):
                markup = p["reply_markup"]
                markup = json.loads(markup) if isinstance(markup, str) else markup
                return [b for row in markup["inline_keyboard"] for b in row]
        return []


class Chat:
    """Simula l'utente: manda testi e preme pulsanti."""

    def __init__(self, app, request):
        self.app, self.request, self._uid = app, request, 0

    def _base(self):
        self._uid += 1
        user = {"id": USER_ID, "is_bot": False, "first_name": "Tester"}
        chat = {"id": CHAT_ID, "type": "private"}
        return user, chat

    async def say(self, text):
        user, chat = self._base()
        message = {"message_id": self._uid, "date": 0, "chat": chat, "from": user, "text": text}
        if text.startswith("/"):
            message["entities"] = [
                {"type": "bot_command", "offset": 0, "length": len(text.split()[0])}
            ]
        await self._send({"update_id": self._uid, "message": message})

    async def press(self, label_contains):
        (button,) = [b for b in self.request.last_keyboard() if label_contains in b["text"]]
        await self.press_data(button["callback_data"])

    async def press_data(self, data):
        user, chat = self._base()
        await self._send({"update_id": self._uid, "callback_query": {
            "id": str(self._uid), "from": user, "chat_instance": "ci", "data": data,
            "message": {"message_id": 1, "date": 0, "chat": chat, "text": "x"},
        }})

    async def _send(self, payload):
        await self.app.process_update(Update.de_json(payload, self.app.bot))
        await asyncio.sleep(0)


@pytest.fixture
async def chat(tmp_path, monkeypatch):
    path = str(tmp_path / "flow.db")
    await init_db.init_db(path)
    monkeypatch.setattr(notifica, "DB_PATH", path)

    async def load_catalog_tree(force=False):
        return CATS

    async def search_brands(text, limit=6):
        if text == "boom":
            raise RuntimeError("vinted giù")
        if text.lower() != "adidas":
            return []
        return [{"id": 14, "title": "adidas", "count": "48.7M"},
                {"id": 194976, "title": "adidas Originals", "count": ""}]

    async def get_sizes(catalog_id, max_sizes=90):
        return [{"id": 207, "title": "S"}, {"id": 208, "title": "M"}, {"id": 209, "title": "L"}]

    seeded = []

    async def seed_and_report(bot, alert_id, chat_id, url):
        seeded.append((alert_id, chat_id, url))

    monkeypatch.setattr(notifica.vf, "load_catalog_tree", load_catalog_tree)
    monkeypatch.setattr(notifica.vf, "search_brands", search_brands)
    monkeypatch.setattr(notifica.vf, "get_sizes", get_sizes)
    monkeypatch.setattr(notifica, "seed_and_report", seed_and_report)

    request = FakeRequest()
    app = (ApplicationBuilder().token("123456:TEST").request(request)
           .get_updates_request(FakeRequest()).build())
    other = []

    async def lista(update, context):
        other.append("lista")

    app.add_handler(CommandHandler("lista", lista))
    notifica.register(app)
    await app.initialize()
    c = Chat(app, request)
    c.db_path, c.seeded, c.other = path, seeded, other
    yield c
    await app.shutdown()


async def _alerts(path):
    async with aiosqlite.connect(path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT chat_id, label, search_url, seeded FROM alerts") as cur:
            return [dict(r) for r in await cur.fetchall()]


async def _match_texts(path):
    async with aiosqlite.connect(path) as db:
        async with db.execute("SELECT match_text FROM alerts ORDER BY id") as cur:
            return [r[0] for r in await cur.fetchall()]


async def test_guided_flow_creates_alert_with_all_filters(chat):
    await chat.say("/notifica")
    assert "1/3 · Cosa cerchi" in chat.request.texts()[-1]

    await chat.say("jeans skinny uomo")
    labels = [b["text"] for b in chat.request.last_keyboard()]
    assert labels[0] == "Uomo › Vestiti › Jeans › Jeans skinny"
    assert labels[-2:] == ["🔎 Prodotto «jeans skinny uomo» (nome esatto)", "⏭ Qualsiasi categoria"]
    await chat.press("Jeans skinny")
    assert "2/3 · Brand" in chat.request.texts()[-1]

    await chat.say("adidas")
    await chat.press("adidas (48.7M annunci)")
    assert "3/3 · Taglia" in chat.request.texts()[-1]

    await chat.press("S")
    await chat.press("M")
    await chat.press("L")
    await chat.press("L")          # ripensamento: toglie la L
    selected = [b["text"] for b in chat.request.last_keyboard() if b["text"].startswith("✅ ")]
    assert selected == ["✅ S", "✅ M", "✅ Fatto"]
    await chat.press("Fatto")

    summary = chat.request.texts()[-1]
    assert "Categoria: Uomo › Vestiti › Jeans › Jeans skinny" in summary
    assert "Brand: adidas" in summary and "Taglia: S, M" in summary
    await chat.press("Attiva notifiche")

    expected_url = ("https://www.vinted.it/catalog?catalog[]=1817&brand_ids[]=14"
                    "&size_ids[]=207&size_ids[]=208&order=newest_first")
    assert await _alerts(chat.db_path) == [{
        "chat_id": CHAT_ID, "label": "Jeans skinny (Uomo) · adidas · Taglia S, M",
        "search_url": expected_url, "seeded": 0,
    }]
    assert chat.seeded == [(1, CHAT_ID, expected_url)]   # prima lettura avviata in background
    assert await _match_texts(chat.db_path) == [None]    # nessun filtro sul titolo
    assert "Notifica attivata" in chat.request.texts()[-1]

    # la conversazione è chiusa: un testo libero non viene più interpretato
    n = len(chat.request.calls)
    await chat.say("ciao")
    assert len(chat.request.calls) == n


async def test_pasted_link_goes_straight_to_confirmation(chat):
    await chat.say("/notifica")
    await chat.say("https://www.vinted.it/catalog?catalog[]=1817&price_to=30&order=relevance&page=2")
    assert "Ricerca dal tuo link: Jeans skinny (Uomo) · max 30€" in chat.request.texts()[-1]
    await chat.press("Attiva")
    (alert,) = await _alerts(chat.db_path)
    assert alert["search_url"] == \
        "https://www.vinted.it/catalog?catalog[]=1817&price_to=30&order=newest_first"

    # stessa ricerca una seconda volta, anche passando il link come argomento
    await chat.say("/notifica https://www.vinted.it/catalog?catalog[]=1817&price_to=30")
    await chat.press("Attiva")
    assert "Hai già una notifica attiva" in chat.request.texts()[-1]
    assert len(await _alerts(chat.db_path)) == 1


async def test_bad_link_and_unknown_words_keep_asking(chat):
    await chat.say("/notifica")
    await chat.say("https://www.vinted.it/items/123-jeans")
    assert "Mi serve il link di una ricerca" in chat.request.texts()[-1]
    await chat.say("qwertyuiop")
    assert "Non ho trovato categorie" in chat.request.texts()[-1]
    await chat.say("jeans")                      # ancora nello stato categoria
    assert any("Jeans" in b["text"] for b in chat.request.last_keyboard())


async def test_brand_only_skips_sizes_and_needs_at_least_one_filter(chat):
    await chat.say("/notifica")
    await chat.press("Qualsiasi categoria")
    await chat.press("Qualsiasi brand")
    assert "Serve almeno una categoria o un brand" in chat.request.texts()[-1]

    await chat.press("Qualsiasi categoria")
    await chat.say("boom")
    assert "Non riesco a cercare i brand" in chat.request.texts()[-1]
    await chat.say("marcainesistente")
    assert "Nessun brand trovato" in chat.request.texts()[-1]
    await chat.say("adidas")
    await chat.press("adidas Originals")
    texts = chat.request.texts()
    assert "Senza una categoria non posso proporti le taglie" in texts[-2]
    assert "Brand: adidas Originals" in texts[-1] and "Taglia: qualsiasi" in texts[-1]
    await chat.press("Annulla")
    assert await _alerts(chat.db_path) == []


async def test_cancel_and_other_commands_close_the_setup(chat):
    await chat.say("/annulla")
    assert chat.request.texts()[-1] == "Non c'è nessuna operazione in corso."

    await chat.say("/notifica")
    await chat.say("/annulla")
    texts = chat.request.texts()
    assert texts[-1] == "❌ Annullato: nessuna notifica creata."
    assert texts.count("Non c'è nessuna operazione in corso.") == 1   # non risponde due volte

    await chat.say("/notifica")
    await chat.say("/lista")                    # altro comando: funziona e chiude la configurazione
    assert chat.other == ["lista"]
    n = len(chat.request.calls)
    await chat.say("jeans")
    assert len(chat.request.calls) == n


async def test_stale_buttons_answer_instead_of_spinning(chat):
    await chat.press_data("nc_1817")            # pulsante di una configurazione chiusa
    method, params = chat.request.calls[-1]
    assert method == "answerCallbackQuery" and "non è più attivo" in params["text"]

    await chat.say("/notifica")
    await chat.press_data("nk_ok")              # pulsante di un altro passo
    method, params = chat.request.calls[-1]
    assert method == "answerCallbackQuery" and "non è più attivo" in params["text"]
    assert await _alerts(chat.db_path) == []


async def test_notifiche_lists_and_removes(chat):
    await chat.say("/notifiche")
    assert "Non hai notifiche attive" in chat.request.texts()[-1]

    await chat.say("/notifica https://www.vinted.it/catalog?brand_ids[]=14")
    await chat.press("Attiva")
    await chat.say("/notifiche")
    listing = chat.request.texts()[-1]
    assert "1. 1 brand" in listing and "brand_ids[]=14" in listing
    await chat.press("Rimuovi 1.")
    assert "Notifica rimossa: 1 brand" in chat.request.texts()[-1]
    assert await _alerts(chat.db_path) == []


# --- nome esatto di un prodotto ---------------------------------------------

async def test_product_name_as_command_argument(chat):
    await chat.say("/notifica rtx 5060")
    assert "Non ho trovato categorie Vinted per «rtx 5060»" in chat.request.texts()[-1]
    labels = [b["text"] for b in chat.request.last_keyboard()]
    assert labels == ["🔎 Prodotto «rtx 5060» (nome esatto)", "⏭ Qualsiasi categoria"]

    await chat.press("Prodotto «rtx 5060»")
    texts = chat.request.texts()
    assert texts[-2] == "✅ Prodotto: «rtx 5060» (nome esatto)"
    assert "Prodotto: «rtx 5060» (nome esatto)" in texts[-1]
    assert "tutte queste parole nel titolo" in texts[-1]
    assert "search_text=rtx%205060&amp;order=newest_first" in texts[-1]
    await chat.press("Attiva notifiche")

    expected_url = "https://www.vinted.it/catalog?search_text=rtx%205060&order=newest_first"
    assert await _alerts(chat.db_path) == [{
        "chat_id": CHAT_ID, "label": "Prodotto «rtx 5060»", "search_url": expected_url, "seeded": 0,
    }]
    assert await _match_texts(chat.db_path) == ["rtx 5060"]
    assert chat.seeded == [(1, CHAT_ID, expected_url)]
    assert "Notifica attivata" in chat.request.texts()[-1]

    # stesso prodotto di nuovo (scritto in modo diverso): niente doppione
    await chat.say("/notifica")
    await chat.say('  "rtx   5060" ')
    await chat.press("Prodotto «rtx 5060»")
    await chat.press("Attiva notifiche")
    assert "Hai già una notifica attiva" in chat.request.texts()[-1]
    assert len(await _alerts(chat.db_path)) == 1

    await chat.say("/notifiche")
    assert "1. Prodotto «rtx 5060»" in chat.request.texts()[-1]


async def test_product_name_typed_in_the_guided_flow(chat):
    await chat.say("/notifica")
    await chat.say("jeans")                     # prima un testo, poi ci ripensa
    await chat.say("Logitech MX Vertical")
    await chat.press("Prodotto «Logitech MX Vertical»")
    assert "Prodotto: «Logitech MX Vertical» (nome esatto)" in chat.request.texts()[-1]
    await chat.press("Attiva notifiche")
    (alert,) = await _alerts(chat.db_path)
    assert alert["label"] == "Prodotto «Logitech MX Vertical»"
    assert alert["search_url"] == \
        "https://www.vinted.it/catalog?search_text=Logitech%20MX%20Vertical&order=newest_first"
    assert await _match_texts(chat.db_path) == ["Logitech MX Vertical"]


async def test_product_button_when_text_is_also_a_category(chat):
    await chat.say("/notifica jeans")
    assert "Se invece «jeans» è il nome di un prodotto" in chat.request.texts()[-1]
    await chat.press("Prodotto «jeans»")
    await chat.press("Annulla")
    assert await _alerts(chat.db_path) == []

    # la strada "categoria" resta quella di prima, senza filtro sul titolo
    await chat.say("/notifica jeans")
    await chat.press("Uomo › Vestiti › Jeans › Jeans skinny")
    await chat.press("Qualsiasi brand")
    await chat.press("Qualsiasi taglia")
    await chat.press("Attiva notifiche")
    (alert,) = await _alerts(chat.db_path)
    assert alert["search_url"] == "https://www.vinted.it/catalog?catalog[]=1817&order=newest_first"
    assert await _match_texts(chat.db_path) == [None]


async def test_product_button_not_offered_for_useless_text_and_stale_press(chat):
    await chat.say("/notifica")
    await chat.say("5")
    assert "Non ho trovato categorie per «5»" in chat.request.texts()[-1]
    assert [b["text"] for b in chat.request.last_keyboard()] == ["⏭ Qualsiasi categoria"]
    await chat.say("x" * 80)                    # troppo lungo per essere un nome prodotto
    assert [b["text"] for b in chat.request.last_keyboard()] == ["⏭ Qualsiasi categoria"]

    await chat.press_data("np_ok")              # pulsante vecchio: nessun prodotto in bozza
    assert "non è più valida" in chat.request.texts()[-1]
    await chat.say("/annulla")
    await chat.press_data("np_ok")              # configurazione chiusa
    method, params = chat.request.calls[-1]
    assert method == "answerCallbackQuery" and "non è più attivo" in params["text"]
    assert await _alerts(chat.db_path) == []
