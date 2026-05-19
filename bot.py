import logging
import asyncio
import os
import sqlite3
import random
import re
from datetime import timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from vinted_scraper import VintedScraper
from vinted_market import get_vinted_market_price

from dotenv import load_dotenv

# Carica variabili d'ambiente da .env
load_dotenv()

# Setup logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

DB_PATH = "deals.db"

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

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
        "3️⃣ **Altro:**\n"
        "• `/help` - Mostra questo messaggio.\n\n"
        "💡 *Il bot controlla Vinted ogni 10 minuti e ti invia un link appena trova un'offerta!*"
    )
    await update.message.reply_text(help_text, parse_mode="Markdown")

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
    
    # Calculate market price even if custom price is set, as a reference
    market_price = get_vinted_market_price(query)
    
    if not market_price and not custom_price:
        await update.message.reply_text("Non sono riuscito a trovare abbastanza dati su Vinted e non hai impostato un prezzo manuale.")
        return

    # Use 70% of market price as default if no custom price is set
    effective_max = custom_price if custom_price else (market_price * 0.7 if market_price else None)

    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO items (name, chat_id, market_price, max_price, last_market_update) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)", 
            (query, chat_id, market_price, custom_price)
        )
        conn.commit()
        conn.close()
        
        market_info = f"💰 Prezzo di mercato stimato: {market_price}€\n" if market_price else ""
        price_mode = f"🎯 Prezzo massimo impostato: **{custom_price}€** (Manuale)" if custom_price else f"🚀 Notifiche per offerte sotto i **{round(effective_max, 2)}€** (Auto -30%)"
        
        await update.message.reply_text(
            f"✅ Monitoraggio avviato!\n"
            f"📦 Oggetto: {query}\n"
            f"{market_info}"
            f"{price_mode}",
            parse_mode="Markdown"
        )
    except Exception as e:
        await update.message.reply_text(f"Errore nel database: {e}")

async def lista(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    conn = get_db()
    cursor = conn.cursor()
    items = cursor.execute("SELECT name, market_price, max_price FROM items WHERE chat_id = ? AND is_active = 1", (chat_id,)).fetchall()
    conn.close()
    
    if not items:
        await update.message.reply_text("Non ci sono oggetti in monitoraggio per questa chat.")
        return
    
    text = "📝 **Oggetti in monitoraggio qui:**\n\n"
    for item in items:
        target = item['max_price'] if item['max_price'] else round(item['market_price'] * 0.7, 2)
        mode = "(Manuale)" if item['max_price'] else "(Auto -30%)"
        text += f"• **{item['name']}**\n  Target: {target}€ {mode}\n"
    await update.message.reply_text(text, parse_mode="Markdown")

async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Specifica l'oggetto da fermare.")
        return
    
    query = " ".join(context.args)
    chat_id = update.effective_chat.id
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM items WHERE name = ? AND chat_id = ?", (query, chat_id))
    conn.commit()
    conn.close()
    
    await update.message.reply_text(f"🛑 Monitoraggio fermato per '{query}'.")

async def check_deals_callback(context: ContextTypes.DEFAULT_TYPE):
    conn = get_db()
    cursor = conn.cursor()
    items = cursor.execute("SELECT id, name, market_price, max_price, chat_id FROM items WHERE is_active = 1").fetchall()
    
    scraper = VintedScraper("https://www.vinted.it")
    
    for item in items:
        query = item['name']
        market_price = item['market_price']
        custom_max = item['max_price']
        chat_id = item['chat_id']
        
        # Determine the target price for notification
        threshold = custom_max if custom_max else (market_price * 0.7 if market_price else 0)
        
        if threshold == 0: continue

        try:
            found_items = scraper.search({"search_text": query})
            for f_item in found_items:
                try:
                    price = float(f_item.price)
                except: continue
                
                if price <= threshold:
                    cursor.execute("SELECT id FROM found_deals WHERE url = ?", (f_item.url,))
                    if not cursor.fetchone():
                        cursor.execute("INSERT INTO found_deals (item_id, url, price, platform) VALUES (?, ?, ?, ?)",
                                       (item['id'], f_item.url, price, "Vinted"))
                        conn.commit()
                        
                        saving = f" (Sconto {round((1 - price/market_price)*100, 1)}%)" if market_price else ""
                        msg = (
                            f"🔥 **AFFARE TROVATO!** 🔥\n\n"
                            f"📦 **{f_item.title}**\n"
                            f"💰 Prezzo: **{price}€**{saving}\n"
                            f"🎯 Target: {threshold}€\n\n"
                            f"🔗 [Vedi su Vinted]({f_item.url})"
                        )
                        await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode="Markdown")
        except Exception as e:
            logging.error(f"Errore nel task per {query}: {e}")
            
    conn.close()

if __name__ == '__main__':
    TOKEN = os.getenv("TELEGRAM_TOKEN")
    
    if not TOKEN:
        print("Errore: La variabile d'ambiente TELEGRAM_TOKEN non è impostata.")
        exit(1)

    app = ApplicationBuilder().token(TOKEN).build()
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cerca", cerca))
    app.add_handler(CommandHandler("lista", lista))
    app.add_handler(CommandHandler("stop", stop))
    
    job_queue = app.job_queue
    job_queue.run_repeating(check_deals_callback, interval=600, first=10)
    
    print("Bot avviato...")
    app.run_polling()
