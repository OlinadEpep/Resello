import logging
import asyncio
import os
import time
import aiosqlite
import random
import re
from datetime import timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, CallbackQueryHandler
from vinted_scraper import AsyncVintedScraper
from vinted_playwright import VintedPlaywrightScraper
from vinted_market import get_vinted_market_price
from init_db import init_db
import notifica
from utils import (
    is_valid_item,
    calculate_min_price,
    fetch_item_details,
    compute_service_fee,
    DEFAULT_SHIPPING,
)

from dotenv import load_dotenv

# Carica variabili d'ambiente da .env
load_dotenv()

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

DB_PATH = "deals.db"

# Idea 3 — throttle mercato: riusa prezzo fresco invece di riscrapeare.
# Scelta utente: TTL 6h (env MARKET_TTL_HOURS per override 6-24).
try:
    MARKET_TTL_HOURS = float(os.getenv("MARKET_TTL_HOURS", "6"))
except ValueError:
    MARKET_TTL_HOURS = 6.0

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Ciao! Sono Deal Hunter Bot, il tuo cacciatore di affari personalizzato.\n\n"
        "Usa /help per vedere tutti i comandi disponibili e imparare a usarmi al meglio!"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "🕵️‍♂️ **Guida ai Comandi del Deal Hunter**\n\n"
        "1️⃣ **Aggiungere una ricerca:**\n"
        "• `/cerca [oggetto]`\n"
        "  Il bot calcola il prezzo di mercato e ti avvisa se trova uno sconto del 30%.\n"
        "  *Esempio: `/cerca Nintendo 3DS`*\n\n"
        "• `/cerca [oggetto] | [prezzo_max]`\n"
        "  Imposta un tetto massimo di prezzo personalizzato.\n"
        "  *Esempio: `/cerca Nintendo 3DS | 80`*\n\n"
        "2️⃣ **Gestione ricerche:**\n"
        "• `/lista` - Mostra tutto quello che stai monitorando.\n"
        "• `/stop [oggetto]` - Ferma il monitoraggio di un oggetto.\n\n"
        "3️⃣ **Notifiche nuovi annunci:**\n"
        "• `/notifica` - Scegli categoria, brand e taglia: ti avviso per ogni "
        "nuovo annuncio pubblicato con quei filtri (qualsiasi prezzo).\n"
        "• `/notifica [nome prodotto]` - Ti avviso per ogni nuovo annuncio che ha "
        "quel nome esatto nel titolo.\n"
        "  *Esempio: `/notifica logitech mx vertical`*\n"
        "• `/notifiche` - Mostra le notifiche attive e permette di rimuoverle.\n"
        "• `/annulla` - Esce dalla configurazione di una notifica.\n\n"
        "4️⃣ **Altro:**\n"
        "• `/help` - Mostra questo messaggio.\n"
        "• `/funzionamento` - Capire come ragiona il bot.\n\n"
        "💡 *Il bot controlla le offerte ogni 10 minuti e i nuovi annunci delle "
        "notifiche ogni paio di minuti.*"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

async def funzionamento(update: Update, context: ContextTypes.DEFAULT_TYPE):
    part1 = (
        "⚙️ **Come funziona Deal Hunter — parte 1/2: ricerca e prezzo**\n\n"
        "1️⃣ **Ricerca iniziale**\n"
        "Quando usi `/cerca`, il bot interroga Vinted con le tue parole e raccoglie "
        "gli annunci trovati: titolo, prezzo e link di ognuno.\n\n"
        "2️⃣ **Stima del prezzo di mercato**\n"
        "Dai prezzi raccolti calcola la mediana, scartando prima i valori estremi "
        "più alti e più bassi che falserebbero la media. Al prezzo base aggiunge "
        "sempre i costi reali: protezione acquisti e spedizione stimata, così il "
        "confronto è sul prezzo totale che pagheresti davvero.\n\n"
        "3️⃣ **Soglia dell'affare**\n"
        "Di default un annuncio è interessante se costa almeno il 30% in meno del "
        "mercato. Con `/cerca oggetto | prezzo` decidi tu il tetto massimo. "
        "C'è anche una soglia minima: prezzi troppo bassi rispetto al mercato "
        "sono quasi sempre scatole vuote o pezzi di ricambio, quindi vengono scartati.\n\n"
        "4️⃣ **Memoria del mercato**\n"
        "Il prezzo stimato viene salvato e riusato per 6 ore: se ripeti la stessa "
        "ricerca non rifà tutto da capo, e in background aggiorna solo le ricerche "
        "con dati vecchi. Così è più veloce e interroga Vinted solo quando serve."
    )
    part2 = (
        "⚙️ **Come funziona Deal Hunter — parte 2/2: selezione e notifiche**\n\n"
        "5️⃣ **Controllo ogni 10 minuti**\n"
        "Per ogni cosa che monitori, il bot ricontrolla gli annunci più recenti. "
        "Scarta subito quelli sopra soglia e quelli già segnalati, poi per i "
        "rimanenti apre la pagina dell'annuncio e legge il prezzo totale vero: "
        "articolo più spedizione più protezione.\n\n"
        "6️⃣ **Filtro dei falsi positivi**\n"
        "Prima un filtro veloce a parole chiave elimina rotture, ricambi e pezzi "
        "di ricambio. Poi un filtro statistico impara da solo: se nota che gli "
        "annunci molto economici contengono spesso le stesse parole, come custodie "
        "o manuali, le aggiunge ai filtri. Infine un giudizio semantico sul titolo "
        "capisce se l'annuncio vende davvero il prodotto intero o solo un accessorio, "
        "una scatola vuota o un oggetto rotto, anche in altre lingue.\n\n"
        "7️⃣ **Scelta di cosa segnalarti**\n"
        "Per non farti perdere tempo con decine di pagine, gli annunci candidati "
        "vengono ordinati per pertinenza e solo i migliori arrivano al controllo "
        "completo. Se il giudizio è incerto, ti avviso comunque ma con la scritta "
        "⚠️ Da verificare, così decidi tu con le foto davanti.\n\n"
        "8️⃣ **Notifica**\n"
        "Se tutto torna ricevi il messaggio con prezzo totale, sconto sul mercato "
        "e link diretto. Ogni offerta viene segnata come già vista, quindi niente "
        "doppioni. Usa `/lista` per vedere le ricerche e `/stop` per fermarle."
    )
    await update.message.reply_text(part1, parse_mode="Markdown")
    await update.message.reply_text(part2, parse_mode="Markdown")

async def cerca(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Specifica cosa vuoi cercare. Es: `/cerca Nintendo 3DS` o `/cerca Nintendo 3DS | 80`", parse_mode="Markdown")
        return

    full_query = " ".join(context.args)
    custom_price = None
    
    # Check if user specified a custom price using the '|' separator
    if "|" in full_query:
        parts = full_query.split("|")
        query = parts[0].strip()
        try:
            custom_price = float(re.sub(r'[^\d.]', '', parts[1].strip()))
        except ValueError:
            await update.message.reply_text("❌ Il prezzo inserito dopo '|' non è valido. Usa un numero, es: `| 80`", parse_mode="Markdown")
            return
    else:
        query = full_query.strip()

    chat_id = update.effective_chat.id

    await update.message.reply_text(f"🔍 Elaboro la ricerca per '{query}'...")

    # Idea 3: se esiste un prezzo fresco (<TTL) per questa (query, chat), riusalo.
    market_price, dynamic_keywords, reused = None, [], False
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT market_price, negative_keywords, last_market_update FROM items "
                "WHERE name = ? AND chat_id = ?",
                (query, chat_id),
            ) as cur:
                row = await cur.fetchone()
            if row and row["market_price"]:
                try:
                    import datetime as _dt
                    ts = _dt.datetime.fromisoformat(str(row["last_market_update"])) \
                        if row["last_market_update"] else None
                    age_h = (_dt.datetime.now() - ts).total_seconds() / 3600 if ts else 999
                except Exception:
                    age_h = 999
                if age_h < MARKET_TTL_HOURS:
                    market_price = row["market_price"]
                    dynamic_keywords = row["negative_keywords"].split(",") \
                        if row["negative_keywords"] else []
                    reused = True
                    logger.info(f"Market cache hit per '{query}' (età {age_h:.1f}h < {MARKET_TTL_HOURS}h)")
    except Exception as e:
        logger.debug(f"Market cache lookup fallita: {e}")

    if not reused:
        # Calculate market price and extract dynamic negative keywords.
        # vinted_market ritorna (price, kws, meta); compat con 2-tuple legacy.
        res = await get_vinted_market_price(query)
        if not res:
            market_price, dynamic_keywords, market_meta = None, [], {}
        elif len(res) == 3:
            market_price, dynamic_keywords, market_meta = res[0], res[1] or [], res[2] or {}
        else:
            market_price, dynamic_keywords, market_meta = res[0], res[1] or [], {}
        if market_meta.get('suggested_query'):
            logger.warning(f"Market '{query}' scartato ({market_meta.get('sanity')}), "
                           f"suggerimento: '{market_meta.get('suggested_query')}'")
        if market_meta:
            logger.info(f"Market meta '{query}': {market_meta}")
    
    if not market_price and not custom_price:
        await update.message.reply_text("Non sono riuscito a recuperare dati di mercato da Vinted. Riprova o imposta un prezzo manuale con `| prezzo`.")
        return

    # Use 70% of market price as default if no custom price is set
    effective_max = custom_price if custom_price else (market_price * 0.7 if market_price else None)
    min_price = calculate_min_price(market_price, effective_max)
    
    neg_kws_str = ",".join(dynamic_keywords) if dynamic_keywords else None

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO items (name, chat_id, market_price, max_price, negative_keywords, last_market_update) VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)", 
                (query, chat_id, market_price, custom_price, neg_kws_str)
            )
            await db.commit()
        
        market_info = f"💰 Prezzo di mercato stimato (totale): {market_price}€ (inclusi spedizione e protezione)\n" if market_price else ""
        if reused:
            market_info = (f"💰 Prezzo di mercato (cache {MARKET_TTL_HOURS:g}h): {market_price}€ "
                           f"(inclusi spedizione e protezione)\n") if market_price else market_info
        neg_info = f"🚫 Filtri auto: {', '.join(dynamic_keywords)}\n" if dynamic_keywords else ""
        if custom_price:
            price_mode = f"🎯 Range di prezzo totale monitorato: **{round(min_price, 2)}€** - **{custom_price}€** (Manuale)"
        else:
            price_mode = f"🚀 Offerte target totali: tra **{round(min_price, 2)}€** e **{round(effective_max, 2)}€** (Auto -30% su totale)"
        
        await update.message.reply_text(
            f"✅ Monitoraggio avviato su Vinted!\n"
            f"📦 Oggetto: {query}\n"
            f"{market_info}"
            f"{neg_info}"
            f"{price_mode}\n"
            f"💡 *I prezzi includono il costo di spedizione e la Protezione Acquisti Vinted.*",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Database error in cerca: {e}")
        await update.message.reply_text(f"Errore nel database: {e}")

async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT id, name, market_price, max_price FROM items WHERE chat_id = ? AND is_active = 1", (chat_id,)) as cursor:
            items = await cursor.fetchall()
    
    if not items:
        await update.message.reply_text("Non ci sono oggetti in monitoraggio per questa chat.")
        return
    
    text = "📝 **Oggetti in monitoraggio qui (prezzi totali con spedizione):**\n\n"
    keyboard = []
    for item in items:
        target = item['max_price'] if item['max_price'] else round(item['market_price'] * 0.7, 2)
        min_price = calculate_min_price(item['market_price'], target)
        mode = "M" if item['max_price'] else "A"
        text += f"• **{item['name']}** - Range: {min_price}€ - {target}€ ({mode})\n"
        keyboard.append([InlineKeyboardButton(f"🛑 Stop {item['name'][:20]}", callback_data=f"stop_{item['id']}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=reply_markup)

async def stop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    item_id = query.data.split("_")[1]
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT name FROM items WHERE id = ?", (item_id,)) as cursor:
            item = await cursor.fetchone()
        if item:
            await db.execute("DELETE FROM items WHERE id = ?", (item_id,))
            await db.commit()
            await query.edit_message_text(text=f"🛑 Monitoraggio fermato per '{item['name']}'.")
        else:
            await query.edit_message_text(text="Oggetto non trovato o già rimosso.")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Specifica l'oggetto da fermare o usa /lista per vedere i pulsanti.")
        return
    
    query = " ".join(context.args)
    chat_id = update.effective_chat.id
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM items WHERE name = ? AND chat_id = ?", (query, chat_id))
        await db.commit()
    
    await update.message.reply_text(f"🛑 Monitoraggio fermato per '{query}'.")

async def _maybe_refresh_market(db, items):
    """Idea 3: aggiorna in background solo gli item con mercato scaduto (>TTL).

    Fail-open: se lo scrape fallisce si tiene il vecchio prezzo.
    Ritorna dict {item_id: (market_price, dynamic_kws)} aggiornato per i soli
    item rinfrescati con successo.
    """
    import datetime as _dt
    import random as _rnd

    stale = []
    now = _dt.datetime.now()
    for item in items:
        try:
            ts = _dt.datetime.fromisoformat(str(item["last_market_update"])) \
                if item["last_market_update"] else None
            age_h = (now - ts).total_seconds() / 3600 if ts else 999
        except Exception:
            age_h = 999
        if age_h >= MARKET_TTL_HOURS:
            stale.append(item)

    if not stale:
        return {}
    logger.info(f"Market refresh: {len(stale)} ricerche scadute (TTL {MARKET_TTL_HOURS}h)")
    sem = asyncio.Semaphore(2)
    refreshed = {}

    async def _one(item):
        async with sem:
            await asyncio.sleep(_rnd.uniform(0, 2))  # jitter anti-stampede
            try:
                res = await get_vinted_market_price(item["name"])
                if not res:
                    return
                price = res[0] if len(res) >= 1 else None
                kws = res[1] if len(res) >= 2 else []
                meta = res[2] if len(res) >= 3 else {}
                if meta.get('suggested_query'):
                    logger.warning(f"Market refresh '{item['name']}' scartato "
                                   f"({meta.get('sanity')})")
                    return
                if price:
                    neg = ",".join(kws) if kws else None
                    await db.execute(
                        "UPDATE items SET market_price = ?, negative_keywords = ?, "
                        "last_market_update = CURRENT_TIMESTAMP WHERE id = ?",
                        (price, neg, item["id"]),
                    )
                    await db.commit()
                    refreshed[item["id"]] = (price, kws or [])
                    logger.info(f"Market refresh ok '{item['name']}': {price}€")
            except Exception as e:
                logger.warning(f"Market refresh fallito '{item['name']}': {e} (tengo vecchio prezzo)")

    await asyncio.gather(*(_one(it) for it in stale))
    return refreshed


async def check_deals_callback(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Avvio controllo offerte...")
    t_start = time.monotonic()
    import httpx

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, name, market_price, max_price, chat_id, negative_keywords, "
            "last_market_update FROM items WHERE is_active = 1"
        ) as cursor:
            items = await cursor.fetchall()

        if not items:
            return

        # Idea 3: refresh mercato scaduto prima del ciclo (fail-open)
        try:
            refreshed = await _maybe_refresh_market(db, items)
            if refreshed:
                merged = []
                for it in items:
                    if it["id"] in refreshed:
                        price, kws = refreshed[it["id"]]
                        d = dict(it)
                        d["market_price"] = price
                        d["negative_keywords"] = ",".join(kws) if kws else it["negative_keywords"]
                        merged.append(d)
                    else:
                        merged.append(it)
                items = merged
        except Exception as e:
            logger.warning(f"Market refresh ciclo saltato: {e}")

        # FIX: create() ritorna una coroutine -> await, poi async with sull'istanza
        # per chiudere correttamente l'httpx client interno.
        v_scraper = await AsyncVintedScraper.create("https://www.vinted.it")
        vp_scraper = VintedPlaywrightScraper()
        await vp_scraper.start()
        try:
            async with v_scraper:
                async with httpx.AsyncClient(follow_redirects=True) as http_client:
                    for item in items:
                        try:
                            await _check_single_item(
                                db, item, v_scraper, vp_scraper, http_client, context
                            )
                        except Exception as e:
                            logger.error(f"Errore check '{item['name']}': {e}", exc_info=True)
        finally:
            await vp_scraper.close()

    logger.info(f"Ciclo offerte completato in {time.monotonic()-t_start:.1f}s per {len(items)} ricerche.")


async def _check_single_item(db, item, v_scraper, vp_scraper, http_client, context):
    query = item['name']
    market_price = item['market_price']
    custom_max = item['max_price']
    chat_id = item['chat_id']
    threshold = custom_max if custom_max else (market_price * 0.7 if market_price else 0)
    min_price = calculate_min_price(market_price, threshold)

    dynamic_kws = item['negative_keywords'].split(',') if item['negative_keywords'] else []

    if not threshold:
        return

    # Search Vinted (FIX: mancava await)
    found_vinted = []
    try:
        found_vinted = await v_scraper.search({"search_text": query})
    except Exception as e:
        logger.error(f"Errore Vinted library search per {query}: {e}")

    if not found_vinted:
        logger.info(f"No results for {query} via library, trying Playwright fallback...")
        try:
            found_vinted = await vp_scraper.search_deals(
                query, threshold, min_price=min_price, dynamic_keywords=dynamic_kws
            )
        except Exception as e:
            logger.error(f"Errore Playwright search per {query}: {e}")

    if not found_vinted:
        return

    # Normalize items to list of dictionaries
    normalized_items = []
    for f_item in found_vinted:
        if isinstance(f_item, dict):
            try:
                normalized_items.append({
                    'title': f_item['title'],
                    'price': float(f_item['price']),
                    'url': f_item['url']
                })
            except (KeyError, TypeError, ValueError):
                continue
        else:
            try:
                normalized_items.append({
                    'title': f_item.title,
                    'price': float(f_item.price),
                    'url': f_item.url
                })
            except Exception:
                continue

    # Prefiltro base-price + deduplica PRIMA del fetch dettagli (risparmio banda)
    candidates = []
    for norm_item in normalized_items:
        if norm_item['price'] > threshold:
            continue
        async with db.execute("SELECT id FROM found_deals WHERE url = ?", (norm_item['url'],)) as cur:
            if await cur.fetchone():
                continue
        candidates.append(norm_item)

    if not candidates:
        return

    # Giudizio Jev opzionale (lazy import: se manca chiave/SDK -> solo regex)
    jev = None
    try:
        from deal_judge import get_judge
        jev = get_judge()
    except Exception as e:
        logger.debug(f"Jev judge non disponibile, uso solo regex: {e}")

    # Idea 1 — rerank pre-fetch: con molti candidati, un Noul leggero per
    # coppia (query, titolo) riordina e taglia a top_k=8 prima dei fetch
    # dettagli (lenti, spesso Playwright). Fail-open: se fallisce, tutti.
    if jev is not None:
        try:
            from deal_judge import RERANK_ENABLED, RERANK_TOP_K
            if RERANK_ENABLED and len(candidates) > 4:
                ranked = await jev.rank_candidates(query, candidates)
                if ranked:
                    logger.info(
                        f"Rerank '{query}': {len(candidates)} candidati -> "
                        f"{len(ranked)} fetch (top_k={RERANK_TOP_K})"
                    )
                    candidates = [c for (c, _s) in ranked]
        except Exception as e:
            logger.warning(f"Rerank saltato per '{query}': {e}")

    # Fetch dettagli in parallelo con semaforo (max 4 concorrenti)
    sem = asyncio.Semaphore(4)

    async def _fetch_one(norm_item):
        async with sem:
            title = norm_item['title']
            base_price = norm_item['price']
            url = norm_item['url']
            logger.info(f"Candidate deal: fetch dettagli per: {title} ({base_price}€)")
            details = await fetch_item_details(
                url, playwright_scraper=vp_scraper, http_client=http_client
            )
            if details and details.get('total_price') is not None:
                total_price = details['total_price']
                shipping_price = details.get('shipping_price', DEFAULT_SHIPPING)
                service_fee = details.get('service_fee', compute_service_fee(base_price))
                if details.get('base_price'):
                    base_price = details['base_price']
            else:
                service_fee = compute_service_fee(base_price)
                shipping_price = DEFAULT_SHIPPING
                total_price = round(base_price + service_fee + shipping_price, 2)
                logger.warning(f"Could not fetch details for {url}, using fallback price {total_price}€")
            return {
                'title': title, 'base_price': base_price, 'url': url,
                'total_price': total_price, 'shipping_price': shipping_price,
                'service_fee': service_fee,
            }

    detailed = await asyncio.gather(*(_fetch_one(c) for c in candidates))

    for d in detailed:
        if d['total_price'] > threshold:
            continue
        if not is_valid_item(
            d['title'], d['total_price'], dynamic_keywords=dynamic_kws,
            for_market_calc=False, min_price=min_price,
        ):
            continue
        # Filtro semantico Jev: scarta accessori/rotti, flagga incerti
        # Scelta utente (3): i review notificano comunque ma marcati "da verificare".
        needs_review = False
        if jev is not None:
            try:
                verdict = await jev.judge(
                    query=query, title=d['title'],
                    total_price=d['total_price'], market_price=market_price,
                    threshold=threshold, url=d['url'],
                )
                if verdict.get('decision') == 'skip':
                    logger.info(f"Jev skip per '{d['title']}': {verdict.get('reason')}")
                    continue
                if verdict.get('decision') == 'review':
                    logger.info(f"Jev review per '{d['title']}': {verdict.get('reason')}")
                    needs_review = True
            except Exception as e:
                logger.warning(f"Jev judge error per '{d['title']}': {e} -> fallback regex ok")

        await notify_deal(
            db, chat_id, item['id'], d['title'],
            d['total_price'], d['base_price'], d['shipping_price'], d['service_fee'],
            d['url'], "Vinted", market_price, threshold, context,
            needs_review=needs_review,
        )



async def notify_deal(db, chat_id, item_id, title, total_price, base_price, shipping_price, service_fee, url, platform, market_price, threshold, context, needs_review=False):
    import html
    async with db.execute("SELECT id FROM found_deals WHERE url = ?", (url,)) as cursor:
        if not await cursor.fetchone():
            await db.execute("INSERT INTO found_deals (item_id, url, price, platform) VALUES (?, ?, ?, ?)",
                           (item_id, url, total_price, platform))
            await db.commit()

            saving = f" (Sconto {round((1 - total_price/market_price)*100, 1)}%)" if market_price else ""
            icon = "👕" if platform == "Vinted" else "📦"
            review_banner = "⚠️ <b>Da verificare</b> (Jev incerto) — controlla bene annuncio e foto.\n" if needs_review else ""
            
            safe_title = html.escape(title)
            safe_platform = html.escape(platform)
            safe_url = html.escape(url)
            
            msg = (
                f"🔥 <b>AFFARE TROVATO SU {safe_platform.upper()}!</b> {icon}\n"
                f"{review_banner}"
                f"📦 <b>{safe_title}</b>\n"
                f"💰 Prezzo Totale: <b>{total_price}€</b>{saving}\n"
                f"   (Articolo: {base_price}€ | Sped.: {shipping_price}€ | Protezione: {service_fee}€)\n"
                f"🎯 Target Totale: {threshold}€\n\n"
                f"🔗 <a href=\"{safe_url}\">Vedi su {platform}</a>"
            )
            try:
                await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="HTML")
                # Wait 1.5 seconds between notifications to avoid Telegram's rate limit
                await asyncio.sleep(1.5)
            except Exception as e:
                logger.error(f"Errore invio messaggio a {chat_id}: {e}")

# Elenco mostrato da Telegram quando si digita "/" nella chat.
BOT_COMMANDS = [
    BotCommand("cerca", "Monitora un oggetto: avviso se sotto prezzo"),
    BotCommand("lista", "Oggetti monitorati con /cerca"),
    BotCommand("stop", "Ferma il monitoraggio di un oggetto"),
    BotCommand("notifica", "Avviso per ogni nuovo annuncio (filtri o nome prodotto)"),
    BotCommand("notifiche", "Notifiche nuovi annunci attive"),
    BotCommand("annulla", "Esci dalla configurazione di una notifica"),
    BotCommand("funzionamento", "Come ragiona il bot"),
    BotCommand("help", "Guida ai comandi"),
    BotCommand("start", "Messaggio di benvenuto"),
]


async def post_init(application):
    """Eseguito una volta all'avvio, prima di iniziare a ricevere messaggi."""
    # Crea le tabelle mancanti (idempotente): così un aggiornamento del bot
    # non richiede di lanciare init_db.py a mano.
    await init_db()
    try:
        await application.bot.set_my_commands(BOT_COMMANDS)
    except Exception as e:
        logger.warning(f"Impossibile registrare il menu comandi su Telegram: {e}")


def build_application(token):
    app = ApplicationBuilder().token(token).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cerca", cerca))
    app.add_handler(CommandHandler("lista", lista))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("funzionamento", funzionamento))
    app.add_handler(CallbackQueryHandler(stop_callback, pattern="^stop_"))

    app.job_queue.run_repeating(check_deals_callback, interval=600, first=10)

    # /notifica, /notifiche, /annulla + controllo periodico nuovi annunci
    notifica.register(app)
    return app


if __name__ == '__main__':
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    
    if not TOKEN:
        logger.error("TELEGRAM_TOKEN non impostata.")
        exit(1)

    app = build_application(TOKEN)

    logger.info("Bot avviato...")
    app.run_polling()
