"""Comando /notifica: avviso per ogni nuovo annuncio Vinted con certi filtri.

Flusso Telegram (ConversationHandler):
    /notifica -> categoria -> brand -> taglie -> riepilogo -> attiva
In qualsiasi momento del primo passo si può incollare direttamente il link
di una ricerca Vinted già filtrata.

Ogni ricerca salvata (tabella `alerts`) viene ricontrollata dal job
`check_alerts_callback`: la pagina Vinted ordinata dai più recenti viene
letta con Playwright e gli annunci mai visti (vedi
`vinted_filters.detect_new_items`) vengono notificati con il link
dell'annuncio e quello della ricerca con tutti i filtri attivi.

A differenza di /cerca qui non c'è prezzo di mercato né giudizio Jev:
si notifica *ogni* nuovo annuncio che rispetta i filtri.
"""

import asyncio
import html
import logging
import os
import sqlite3
import warnings
from urllib.parse import urlsplit

import aiosqlite
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.ext import (
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.warnings import PTBUserWarning

import vinted_filters as vf
from init_db import DB_PATH
from vinted_playwright import VintedPlaywrightScraper

logger = logging.getLogger(__name__)


def _env_int(name, default, minimum):
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except ValueError:
        return default


# Ogni quanto ricontrollare le ricerche salvate (secondi).
ALERT_INTERVAL_SECONDS = _env_int("ALERT_INTERVAL_SECONDS", 120, 60)
# Tolleranza sugli id annuncio (vedi vinted_filters.detect_new_items).
# Vinted assegna circa 900.000 id l'ora (misurato a novembre 2026):
# 1.000.000 copre gli annunci che compaiono in ricerca fino a ~1 ora dopo
# essere stati creati.
ALERT_ID_MARGIN = _env_int("ALERT_ID_MARGIN", 1_000_000, 0)
# Massimo di messaggi per ricerca in un singolo controllo (poi un riepilogo).
ALERT_MAX_NOTIFY = _env_int("ALERT_MAX_NOTIFY", 8, 1)
MAX_ALERTS_PER_CHAT = 20
CONVERSATION_TIMEOUT_SECONDS = 15 * 60

NEWEST_FEED_URL = f"{vf.BASE_URL}/catalog?order=newest_first"

CATEGORY, BRAND, SIZE, CONFIRM = range(4)

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
_STATE_KEY = "notifica"


# --------------------------------------------------------------------------
# Conversazione /notifica
# --------------------------------------------------------------------------

def _short(text, limit=58):
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _category_button_text(cat):
    path = cat["path"]
    full = " › ".join(path)
    if len(full) <= 58 or len(path) <= 2:
        return _short(full)
    return _short(f"{path[0]} › … › {path[-1]}")


def _draft(context):
    return context.user_data.setdefault(_STATE_KEY, {})


def _end(context):
    context.user_data.pop(_STATE_KEY, None)
    return ConversationHandler.END


CATEGORY_PROMPT = (
    "🔔 <b>Nuova notifica Vinted</b>\n"
    "Ti scrivo ogni volta che su Vinted esce un nuovo annuncio con i filtri che scegli.\n\n"
    "<b>1/3 · Categoria</b>\n"
    "Scrivi cosa cerchi, ad esempio <i>jeans skinny uomo</i> oppure <i>felpe donna</i>.\n\n"
    "💡 In alternativa incolla il link di una ricerca Vinted con i filtri già impostati "
    "(colore, condizioni, prezzo...).\n\n"
    "/annulla per uscire."
)
BRAND_PROMPT = (
    "<b>2/3 · Brand</b>\n"
    "Scrivi il nome del brand, ad esempio <i>adidas</i>."
)
SIZE_PROMPT = (
    "<b>3/3 · Taglia</b>\n"
    "Tocca le taglie che ti interessano (anche più di una) e poi premi <b>Fatto</b>.\n"
    "Se non ne scegli nessuna vanno bene tutte."
)

_ANY_CATEGORY_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("⏭ Qualsiasi categoria", callback_data="nc_any")]]
)
_ANY_BRAND_KB = InlineKeyboardMarkup(
    [[InlineKeyboardButton("⏭ Qualsiasi brand", callback_data="nb_any")]]
)


async def notifica_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data[_STATE_KEY] = {}
    if context.args:
        # /notifica jeans skinny uomo   oppure   /notifica <link>
        return await _handle_category_text(update, context, " ".join(context.args))
    await update.effective_message.reply_text(
        CATEGORY_PROMPT, parse_mode="HTML", reply_markup=_ANY_CATEGORY_KB
    )
    return CATEGORY


async def category_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await _handle_category_text(update, context, update.effective_message.text or "")


async def _handle_category_text(update, context, text):
    msg = update.effective_message
    text = text.strip()
    if vf.looks_like_url(text):
        url, error = vf.normalize_search_url(text)
        if error:
            await msg.reply_text(f"⚠️ {error}")
            return CATEGORY
        try:
            flat = await vf.load_catalog_tree()
        except Exception:
            flat = None  # la descrizione sarà più generica, il link funziona lo stesso
        draft = _draft(context)
        draft.update({"url": url, "label": vf.describe_search_url(url, flat), "from_link": True})
        return await _show_confirm(msg, context)

    try:
        flat = await vf.load_catalog_tree()
    except Exception as e:
        logger.warning(f"Categorie Vinted non disponibili: {e}")
        await msg.reply_text(
            "⚠️ In questo momento non riesco a scaricare le categorie da Vinted.\n"
            "Riprova tra poco, oppure incolla il link di una ricerca Vinted già filtrata."
        )
        return CATEGORY

    results = vf.search_categories(flat, text)
    if not results:
        await msg.reply_text(
            f"Non ho trovato categorie per «{text}».\n"
            "Prova con parole più semplici (es. jeans, felpe, sneakers uomo) "
            "oppure incolla il link di una ricerca Vinted.",
            reply_markup=_ANY_CATEGORY_KB,
        )
        return CATEGORY

    _draft(context)["cat_options"] = {c["id"]: c for c in results}
    keyboard = [
        [InlineKeyboardButton(_category_button_text(c), callback_data=f"nc_{c['id']}")]
        for c in results
    ]
    keyboard.append([InlineKeyboardButton("⏭ Qualsiasi categoria", callback_data="nc_any")])
    await msg.reply_text(
        "Scegli la categoria (oppure scrivi altre parole per cercare di nuovo):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CATEGORY


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _draft(context)
    choice = query.data.split("_", 1)[1]
    if choice == "any":
        draft["catalog"] = None
        chosen = "qualsiasi"
    else:
        cat = (draft.get("cat_options") or {}).get(int(choice))
        if not cat:
            await query.edit_message_text("Questa scelta non è più valida, scrivi di nuovo la categoria.")
            return CATEGORY
        draft["catalog"] = cat
        chosen = vf.category_label(cat)
    await query.edit_message_text(f"✅ Categoria: {chosen}")
    await query.message.reply_text(BRAND_PROMPT, parse_mode="HTML", reply_markup=_ANY_BRAND_KB)
    return BRAND


async def brand_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    try:
        brands = await vf.search_brands(text)
    except Exception as e:
        logger.warning(f"Ricerca brand Vinted fallita: {e}")
        await msg.reply_text(
            "⚠️ Non riesco a cercare i brand su Vinted in questo momento. "
            "Riprova tra poco oppure continua senza brand.",
            reply_markup=_ANY_BRAND_KB,
        )
        return BRAND
    if not brands:
        await msg.reply_text(
            f"Nessun brand trovato per «{text}». Controlla come è scritto e riprova.",
            reply_markup=_ANY_BRAND_KB,
        )
        return BRAND

    _draft(context)["brand_options"] = {b["id"]: b for b in brands}
    keyboard = []
    for b in brands:
        count = f" ({b['count']} annunci)" if b["count"] else ""
        keyboard.append([InlineKeyboardButton(_short(b["title"] + count), callback_data=f"nb_{b['id']}")])
    keyboard.append([InlineKeyboardButton("⏭ Qualsiasi brand", callback_data="nb_any")])
    await msg.reply_text(
        "Scegli il brand (oppure scrivi un altro nome):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return BRAND


async def brand_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _draft(context)
    choice = query.data.split("_", 1)[1]
    if choice == "any":
        draft["brand"] = None
        chosen = "qualsiasi"
    else:
        brand = (draft.get("brand_options") or {}).get(int(choice))
        if not brand:
            await query.edit_message_text("Questa scelta non è più valida, scrivi di nuovo il brand.")
            return BRAND
        draft["brand"] = brand
        chosen = brand["title"]
    await query.edit_message_text(f"✅ Brand: {chosen}")

    catalog = draft.get("catalog")
    if not catalog and not draft.get("brand"):
        context.user_data[_STATE_KEY] = {}
        await query.message.reply_text(
            "Serve almeno una categoria o un brand, altrimenti ti arriverebbe "
            "ogni annuncio di Vinted 🙂\n\n" + CATEGORY_PROMPT,
            parse_mode="HTML", reply_markup=_ANY_CATEGORY_KB,
        )
        return CATEGORY

    draft["sizes"], draft["selected"] = [], []
    if catalog:
        try:
            draft["sizes"] = await vf.get_sizes(catalog["id"])
        except Exception as e:
            logger.warning(f"Taglie Vinted non disponibili per {catalog['id']}: {e}")
    if draft["sizes"]:
        await query.message.reply_text(
            SIZE_PROMPT, parse_mode="HTML", reply_markup=_size_keyboard(draft)
        )
        return SIZE

    if not catalog:
        await query.message.reply_text(
            "ℹ️ Senza una categoria non posso proporti le taglie: vanno bene tutte."
        )
    return await _show_confirm(query.message, context)


def _size_keyboard(draft):
    sizes, selected = draft["sizes"], set(draft["selected"])
    longest = max(len(s["title"]) for s in sizes)
    cols = 4 if longest <= 6 else 3 if longest <= 12 else 2 if longest <= 20 else 1
    buttons = [
        InlineKeyboardButton(
            ("✅ " if s["id"] in selected else "") + s["title"], callback_data=f"ns_{s['id']}"
        )
        for s in sizes
    ]
    rows = [buttons[i:i + cols] for i in range(0, len(buttons), cols)]
    done = "✅ Fatto" if selected else "⏭ Qualsiasi taglia"
    rows.append([InlineKeyboardButton(done, callback_data="ns_done")])
    return InlineKeyboardMarkup(rows)


async def size_toggled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _draft(context)
    if not draft.get("sizes"):
        return SIZE
    size_id = int(query.data.split("_", 1)[1])
    selected = draft.setdefault("selected", [])
    if size_id in selected:
        selected.remove(size_id)
    elif any(s["id"] == size_id for s in draft["sizes"]):
        selected.append(size_id)
    await query.edit_message_reply_markup(reply_markup=_size_keyboard(draft))
    return SIZE


async def size_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _draft(context)
    names = _selected_size_names(draft)
    await query.edit_message_text(f"✅ Taglia: {', '.join(names) if names else 'qualsiasi'}")
    return await _show_confirm(query.message, context)


def _selected_size_names(draft):
    selected = set(draft.get("selected") or [])
    return [s["title"] for s in draft.get("sizes") or [] if s["id"] in selected]


def _finalize_draft(draft):
    """Completa la bozza guidata con link di ricerca ed etichetta."""
    if draft.get("from_link"):
        return
    catalog, brand = draft.get("catalog"), draft.get("brand")
    sizes = _selected_size_names(draft)
    draft["url"] = vf.build_search_url(
        catalog_id=catalog["id"] if catalog else None,
        brand_id=brand["id"] if brand else None,
        size_ids=[s["id"] for s in draft.get("sizes") or [] if s["id"] in set(draft.get("selected") or [])],
    )
    parts = []
    if catalog:
        path = catalog["path"]
        parts.append(f"{path[-1]} ({path[0]})" if len(path) > 1 else path[-1])
    if brand:
        parts.append(brand["title"])
    if sizes:
        parts.append("Taglia " + ", ".join(sizes))
    draft["label"] = " · ".join(parts)


async def _show_confirm(message, context):
    draft = _draft(context)
    _finalize_draft(draft)
    if draft.get("from_link"):
        summary = f"• Ricerca dal tuo link: {html.escape(draft['label'])}"
    else:
        catalog, brand = draft.get("catalog"), draft.get("brand")
        sizes = _selected_size_names(draft)
        summary = (
            f"• Categoria: {html.escape(vf.category_label(catalog)) if catalog else 'qualsiasi'}\n"
            f"• Brand: {html.escape(brand['title']) if brand else 'qualsiasi'}\n"
            f"• Taglia: {html.escape(', '.join(sizes)) if sizes else 'qualsiasi'}"
        )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Attiva notifiche", callback_data="nk_ok"),
        InlineKeyboardButton("❌ Annulla", callback_data="nk_no"),
    ]])
    await message.reply_text(
        "📋 <b>Riepilogo</b>\n"
        f"{summary}\n\n"
        f"🔎 <a href=\"{html.escape(draft['url'])}\">Apri questa ricerca su Vinted</a>\n\n"
        "Ti avviso per ogni nuovo annuncio pubblicato con questi filtri. Attivo?",
        parse_mode="HTML", reply_markup=keyboard, link_preview_options=_NO_PREVIEW,
    )
    return CONFIRM


async def confirm_ok(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    draft = _draft(context)
    url, label = draft.get("url"), draft.get("label")
    chat_id = update.effective_chat.id
    if not url:
        await query.edit_message_text("Qualcosa è andato storto, riparti con /notifica.")
        return _end(context)

    async with aiosqlite.connect(DB_PATH) as db:
        status, alert_id = await create_alert(db, chat_id, label, url)

    if status == "duplicate":
        await query.edit_message_text(
            "ℹ️ Hai già una notifica attiva per questa stessa ricerca. Vedi /notifiche."
        )
    elif status == "limit":
        await query.edit_message_text(
            f"⚠️ Hai già {MAX_ALERTS_PER_CHAT} notifiche attive: rimuovine una da /notifiche "
            "prima di aggiungerne altre."
        )
    else:
        await query.edit_message_text(
            f"✅ <b>Notifica attivata</b>\n🔔 {html.escape(label)}\n\n"
            "⏳ Sto registrando gli annunci già presenti, così ti avviso solo per quelli nuovi...",
            parse_mode="HTML",
        )
        # In background: la lettura con il browser richiede qualche secondo
        # e non deve bloccare gli altri comandi.
        context.application.create_task(
            seed_and_report(context.bot, alert_id, chat_id, url)
        )
    return _end(context)


async def confirm_no(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Annullato: nessuna notifica creata.")
    return _end(context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("❌ Annullato: nessuna notifica creata.")
    return _end(context)


async def _other_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Un altro comando durante la configurazione la chiude in silenzio
    (il comando viene comunque gestito dal suo handler, nel gruppo 0)."""
    return _end(context)


async def _timeout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop(_STATE_KEY, None)


async def _stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer(
        "Questo pulsante non è più attivo. Usa /notifica per ricominciare.", show_alert=True
    )


async def annulla_idle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/annulla fuori dalla configurazione (dentro, risponde `cancel`)."""
    if _STATE_KEY not in context.user_data:
        await update.effective_message.reply_text("Non c'è nessuna operazione in corso.")


# --------------------------------------------------------------------------
# /notifiche: elenco e rimozione
# --------------------------------------------------------------------------

async def notifiche(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, label, search_url FROM alerts WHERE chat_id = ? AND is_active = 1 ORDER BY id",
            (chat_id,),
        ) as cur:
            alerts = await cur.fetchall()

    if not alerts:
        await update.effective_message.reply_text(
            "Non hai notifiche attive. Creane una con /notifica."
        )
        return

    lines = ["🔔 <b>Notifiche nuovi annunci attive</b>\n"]
    keyboard = []
    for n, a in enumerate(alerts, 1):
        lines.append(
            f"{n}. {html.escape(a['label'])} — "
            f"<a href=\"{html.escape(a['search_url'])}\">apri ricerca</a>"
        )
        keyboard.append([InlineKeyboardButton(
            _short(f"🛑 Rimuovi {n}. {a['label']}", 48), callback_data=f"alertstop_{a['id']}"
        )])
    await update.effective_message.reply_text(
        "\n".join(lines), parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard), link_preview_options=_NO_PREVIEW,
    )


async def alert_stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    alert_id = int(query.data.split("_", 1)[1])
    chat_id = update.effective_chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        label = await delete_alert(db, alert_id, chat_id)
    if label:
        await query.edit_message_text(f"🛑 Notifica rimossa: {label}")
    else:
        await query.edit_message_text("Notifica non trovata o già rimossa.")


# --------------------------------------------------------------------------
# DB
# --------------------------------------------------------------------------

async def create_alert(db, chat_id, label, search_url):
    """Ritorna ('created', id) | ('duplicate', None) | ('limit', None)."""
    async with db.execute(
        "SELECT COUNT(*) FROM alerts WHERE chat_id = ? AND is_active = 1", (chat_id,)
    ) as cur:
        (count,) = await cur.fetchone()
    if count >= MAX_ALERTS_PER_CHAT:
        return "limit", None
    try:
        cur = await db.execute(
            "INSERT INTO alerts (chat_id, label, search_url) VALUES (?, ?, ?)",
            (chat_id, label, search_url),
        )
    except sqlite3.IntegrityError:
        return "duplicate", None
    await db.commit()
    return "created", cur.lastrowid


async def delete_alert(db, alert_id, chat_id):
    """Rimuove la notifica (solo se è di questa chat). Ritorna l'etichetta o None."""
    async with db.execute(
        "SELECT label FROM alerts WHERE id = ? AND chat_id = ?", (alert_id, chat_id)
    ) as cur:
        row = await cur.fetchone()
    if not row:
        return None
    await db.execute("DELETE FROM alert_seen WHERE alert_id = ?", (alert_id,))
    await db.execute("DELETE FROM alerts WHERE id = ?", (alert_id,))
    await db.commit()
    return row[0]


async def _mark_seen(db, alert_id, item_ids):
    await db.executemany(
        "INSERT OR IGNORE INTO alert_seen (alert_id, item_id) VALUES (?, ?)",
        [(alert_id, i) for i in item_ids],
    )


# --------------------------------------------------------------------------
# Controllo periodico
# --------------------------------------------------------------------------

async def fetch_listing(scraper, url):
    """Annunci della prima pagina di una ricerca. None = lettura fallita."""
    content = await scraper.get_page_content(url)
    if not content:
        return None
    parts = urlsplit(url)
    return vf.parse_catalog_items(content, base_url=f"{parts.scheme}://{parts.netloc}")


def format_alert_message(label, search_url, item):
    details = [d for d in (
        item.get("brand"),
        f"Taglia {item['size']}" if item.get("size") else None,
        item.get("condition"),
    ) if d]
    lines = [
        "🆕 <b>Nuovo annuncio su Vinted</b>",
        f"🔔 {html.escape(label)}",
        "",
        f"👕 <b>{html.escape(item['title'])}</b>",
    ]
    if details:
        lines.append("🏷 " + html.escape(" · ".join(details)))
    if item.get("price") is not None:
        price = f"💰 <b>{item['price']:.2f}€</b>"
        if item.get("total_price") is not None:
            price += f" ({item['total_price']:.2f}€ con Protezione acquisti)"
        lines.append(price)
    lines += [
        "",
        f"🔗 <a href=\"{html.escape(item['url'])}\">Vedi l'annuncio</a>",
        f"🔎 <a href=\"{html.escape(search_url)}\">Apri la ricerca con tutti i filtri</a>",
    ]
    return "\n".join(lines)


async def process_alert(db, alert, items, bot, get_global_watermark, pause=1.1):
    """Elabora la pagina letta per una ricerca salvata. Ritorna i messaggi inviati.

    `items` None = lettura fallita (non si tocca nulla). Al primo passaggio
    (seeded = 0) gli annunci presenti vengono solo registrati, senza notifiche.
    """
    if items is None:
        return 0
    alert_id = alert["id"]

    if not alert["seeded"]:
        if items:
            watermark = max(it["id"] for it in items)
        else:
            # Ricerca vuota: come punto di partenza serve l'id più recente
            # di tutto Vinted, altrimenti il primo annuncio "risalito" da
            # chissà quando sembrerebbe nuovo.
            watermark = await get_global_watermark()
            if not watermark:
                return 0  # non so da dove partire: riprovo al prossimo giro
        await _mark_seen(db, alert_id, [it["id"] for it in items])
        await db.execute(
            "UPDATE alerts SET seeded = 1, watermark = ?, last_check = CURRENT_TIMESTAMP "
            "WHERE id = ? AND seeded = 0",
            (watermark, alert_id),
        )
        await db.commit()
        return 0

    async with db.execute("SELECT item_id FROM alert_seen WHERE alert_id = ?", (alert_id,)) as cur:
        seen = {row[0] for row in await cur.fetchall()}
    new, watermark = vf.detect_new_items(items, seen, alert["watermark"], ALERT_ID_MARGIN)

    to_send = new[-ALERT_MAX_NOTIFY:]  # i più recenti, in ordine cronologico
    skipped = len(new) - len(to_send)
    sent = 0
    failed = set()
    for item in to_send:
        try:
            await bot.send_message(
                chat_id=alert["chat_id"],
                text=format_alert_message(alert["label"], alert["search_url"], item),
                parse_mode="HTML",
            )
        except Exception as e:
            # Non lo segno come visto: verrà ritentato al prossimo giro
            # (finché resta entro il margine, quindi non all'infinito).
            logger.error(f"Errore invio notifica a {alert['chat_id']}: {e}")
            failed.add(item["id"])
            continue
        sent += 1
        # Segnato subito: se il ciclo si interrompe non rimando doppioni.
        await _mark_seen(db, alert_id, [item["id"]])
        await db.commit()
        if pause:
            await asyncio.sleep(pause)  # limite Telegram: ~1 messaggio al secondo per chat

    if skipped > 0:
        try:
            await bot.send_message(
                chat_id=alert["chat_id"],
                text=(
                    f"➕ Altri <b>{skipped}</b> nuovi annunci per 🔔 {html.escape(alert['label'])}\n"
                    f"🔎 <a href=\"{html.escape(alert['search_url'])}\">Apri la ricerca con tutti i filtri</a>"
                ),
                parse_mode="HTML", link_preview_options=_NO_PREVIEW,
            )
        except Exception as e:
            logger.error(f"Errore invio riepilogo a {alert['chat_id']}: {e}")

    await _mark_seen(db, alert_id, [it["id"] for it in items if it["id"] not in failed])
    # Sotto la soglia un id non può più essere notificato: inutile ricordarlo.
    await db.execute(
        "DELETE FROM alert_seen WHERE alert_id = ? AND item_id <= ?",
        (alert_id, watermark - ALERT_ID_MARGIN),
    )
    await db.execute(
        "UPDATE alerts SET watermark = ?, last_check = CURRENT_TIMESTAMP WHERE id = ?",
        (watermark, alert_id),
    )
    await db.commit()
    return sent


async def run_alert_cycle(db, alerts, scraper, bot, pause=1.1):
    """Un giro di controllo su tutte le ricerche salvate (browser già avviato)."""
    pages = {}  # stesso link in più chat -> una sola lettura
    global_wm = {}

    async def get_global_watermark():
        if "value" not in global_wm:
            feed = await fetch_listing(scraper, NEWEST_FEED_URL)
            global_wm["value"] = max((it["id"] for it in feed or []), default=0)
        return global_wm["value"]

    sent = 0
    for alert in alerts:
        url = alert["search_url"]
        try:
            if url not in pages:
                pages[url] = await fetch_listing(scraper, url)
            sent += await process_alert(
                db, alert, pages[url], bot, get_global_watermark, pause=pause
            )
        except Exception as e:
            logger.error(f"Errore controllo notifica '{alert['label']}': {e}", exc_info=True)
    return sent


async def _active_alerts(db, alert_id=None):
    db.row_factory = aiosqlite.Row
    sql = ("SELECT id, chat_id, label, search_url, watermark, seeded "
           "FROM alerts WHERE is_active = 1")
    params = ()
    if alert_id is not None:
        sql, params = sql + " AND id = ?", (alert_id,)
    async with db.execute(sql + " ORDER BY id", params) as cur:
        return await cur.fetchall()


async def check_alerts_callback(context: ContextTypes.DEFAULT_TYPE):
    async with aiosqlite.connect(DB_PATH) as db:
        alerts = await _active_alerts(db)
        if not alerts:
            return
        scraper = VintedPlaywrightScraper()
        await scraper.start()
        try:
            sent = await run_alert_cycle(db, alerts, scraper, context.bot)
        finally:
            await scraper.close()
    logger.info(f"Controllo notifiche: {len(alerts)} ricerche, {sent} nuovi annunci segnalati.")


async def seed_and_report(bot, alert_id, chat_id, url):
    """Prima lettura subito dopo la creazione + messaggio di esito all'utente."""
    count = None
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            alerts = await _active_alerts(db, alert_id)
            if alerts and not alerts[0]["seeded"]:
                scraper = VintedPlaywrightScraper()
                await scraper.start()
                try:
                    items = await fetch_listing(scraper, url)
                    if items is not None:
                        await _process_single(db, alerts[0], items, scraper, bot)
                finally:
                    await scraper.close()
            alerts = await _active_alerts(db, alert_id)
            if alerts and alerts[0]["seeded"]:
                async with db.execute(
                    "SELECT COUNT(*) FROM alert_seen WHERE alert_id = ?", (alert_id,)
                ) as cur:
                    (count,) = await cur.fetchone()
    except Exception as e:
        logger.error(f"Prima lettura notifica {alert_id} fallita: {e}", exc_info=True)

    minutes = max(1, round(ALERT_INTERVAL_SECONDS / 60))
    every = "ogni minuto" if minutes == 1 else f"ogni {minutes} minuti"
    if count is None:
        text = (
            "⚠️ Non sono riuscito a leggere Vinted proprio adesso. Non devi fare nulla: "
            f"riprovo da solo al prossimo controllo ({every})."
        )
    elif count == 0:
        text = (
            "👍 Pronto! Al momento non c'è nessun annuncio con questi filtri: "
            f"ti scrivo appena ne esce uno. Controllo {every}."
        )
    else:
        text = (
            f"👍 Pronto! Ho registrato i {count} annunci più recenti già presenti (quelli non te li segnalo): "
            f"da adesso ti scrivo per ogni nuovo annuncio. Controllo {every}."
        )
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logger.error(f"Errore invio esito a {chat_id}: {e}")


async def _process_single(db, alert, items, scraper, bot):
    async def get_global_watermark():
        feed = await fetch_listing(scraper, NEWEST_FEED_URL)
        return max((it["id"] for it in feed or []), default=0)

    return await process_alert(db, alert, items, bot, get_global_watermark)


# --------------------------------------------------------------------------
# Registrazione nell'app Telegram
# --------------------------------------------------------------------------

def build_conversation():
    with warnings.catch_warnings():
        # Avviso noto di PTB per i pulsanti inline con per_message=False:
        # qui è voluto (la conversazione mescola messaggi di testo e pulsanti).
        warnings.simplefilter("ignore", PTBUserWarning)
        return ConversationHandler(
            entry_points=[CommandHandler("notifica", notifica_start)],
            states={
                CATEGORY: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, category_text),
                    CallbackQueryHandler(category_chosen, pattern=r"^nc_(any|\d+)$"),
                ],
                BRAND: [
                    MessageHandler(filters.TEXT & ~filters.COMMAND, brand_text),
                    CallbackQueryHandler(brand_chosen, pattern=r"^nb_(any|\d+)$"),
                ],
                SIZE: [
                    CallbackQueryHandler(size_done, pattern=r"^ns_done$"),
                    CallbackQueryHandler(size_toggled, pattern=r"^ns_\d+$"),
                ],
                CONFIRM: [
                    CallbackQueryHandler(confirm_ok, pattern=r"^nk_ok$"),
                    CallbackQueryHandler(confirm_no, pattern=r"^nk_no$"),
                ],
                ConversationHandler.TIMEOUT: [TypeHandler(Update, _timeout)],
            },
            fallbacks=[
                CommandHandler("annulla", cancel),
                MessageHandler(filters.COMMAND, _other_command),
                CallbackQueryHandler(_stale_button, pattern=r"^n[cbsk]_"),
            ],
            allow_reentry=True,
            conversation_timeout=CONVERSATION_TIMEOUT_SECONDS,
        )


def register(app):
    """Aggancia /notifica, /notifiche e il controllo periodico all'applicazione.

    La conversazione sta nel gruppo 1: così gli altri comandi (gruppo 0)
    continuano a funzionare anche a metà configurazione, e in più la chiudono.
    """
    app.add_handler(CommandHandler("notifiche", notifiche))
    app.add_handler(CommandHandler("annulla", annulla_idle))
    app.add_handler(CallbackQueryHandler(alert_stop_callback, pattern=r"^alertstop_\d+$"))
    app.add_handler(build_conversation(), group=1)
    # Pulsanti di una configurazione ormai chiusa
    app.add_handler(CallbackQueryHandler(_stale_button, pattern=r"^n[cbsk]_"), group=1)
    if app.job_queue:
        app.job_queue.run_repeating(
            check_alerts_callback, interval=ALERT_INTERVAL_SECONDS, first=45
        )
