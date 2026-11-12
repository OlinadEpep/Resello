import aiosqlite
import asyncio
import os

DB_PATH = "deals.db"

async def init_db(db_path=None):
    async with aiosqlite.connect(db_path or DB_PATH) as db:
        # Table for tracked items
        await db.execute('''
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            market_price REAL,
            max_price REAL,
            negative_keywords TEXT,
            last_market_update DATETIME,
            is_active INTEGER DEFAULT 1,
            UNIQUE(name, chat_id)
        )
        ''')

        # Migration logic: check if negative_keywords exists
        async with db.execute("PRAGMA table_info(items)") as cursor:
            columns = [info[1] for info in await cursor.fetchall()]
            if 'negative_keywords' not in columns:
                print("Migrating database: adding negative_keywords column to items table...")
                await db.execute("ALTER TABLE items ADD COLUMN negative_keywords TEXT")

        # Table for found deals to prevent duplicates
        await db.execute('''
        CREATE TABLE IF NOT EXISTS found_deals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER,
            url TEXT NOT NULL UNIQUE,
            price REAL,
            platform TEXT,
            found_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (item_id) REFERENCES items (id)
        )
        ''')

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_active_chat ON items(is_active, chat_id)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_found_deals_url ON found_deals(url)"
        )

        # Cache dei giudizi Jev per non ripagare la stessa inferenza.
        await db.execute('''
        CREATE TABLE IF NOT EXISTS jev_cache (
            url_hash TEXT PRIMARY KEY,
            url TEXT NOT NULL,
            verdict TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_jev_cache_created ON jev_cache(created_at)"
        )

        # Idea 2A — log dei segnali Jev per tuning futuro delle soglie.
        await db.execute('''
        CREATE TABLE IF NOT EXISTS jev_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            query TEXT NOT NULL,
            title TEXT NOT NULL,
            url_hash TEXT NOT NULL,
            base_price REAL,
            total_price REAL,
            market_price REAL,
            threshold REAL,
            pm_choice TEXT,
            pm_conf REAL,
            acc REAL,
            brk REAL,
            box REAL,
            dq REAL,
            dq_conf REAL,
            risk REAL,
            decision TEXT,
            reason TEXT,
            notified INTEGER DEFAULT 0
        )
        ''')
        # Fase 4 — osservabilità stime mercato (guardrail, IQR, sanity).
        await db.execute('''
        CREATE TABLE IF NOT EXISTS market_estimates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            query TEXT NOT NULL,
            market_price REAL,
            n_raw INTEGER,
            n_full INTEGER,
            median_raw REAL,
            reason TEXT,
            meta TEXT
        )
        ''')
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_estimates_query ON market_estimates(query, created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_jev_signals_created ON jev_signals(created_at)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_jev_signals_query ON jev_signals(query, created_at)"
        )
        # /notifica — ricerche salvate (link Vinted con filtri) di cui
        # notificare ogni nuovo annuncio. watermark = id annuncio più alto
        # visto; seeded = 0 finché non è stata letta la situazione iniziale.
        await db.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            search_url TEXT NOT NULL,
            watermark INTEGER DEFAULT 0,
            seeded INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            last_check DATETIME,
            UNIQUE(chat_id, search_url)
        )
        ''')
        # match_text = nome esatto del prodotto (/notifica per nome): se c'è,
        # si notificano solo gli annunci che lo hanno nel titolo.
        async with db.execute("PRAGMA table_info(alerts)") as cursor:
            alert_columns = [row[1] for row in await cursor.fetchall()]
        if "match_text" not in alert_columns:
            await db.execute("ALTER TABLE alerts ADD COLUMN match_text TEXT")
        # Annunci già visti per ogni ricerca (solo quelli recenti: i vecchi
        # vengono potati da notifica.process_alert).
        await db.execute('''
        CREATE TABLE IF NOT EXISTS alert_seen (
            alert_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (alert_id, item_id)
        )
        ''')

        # Cleanup: dimentica deal vecchi >90gg e giudizi >30gg
        try:
            await db.execute(
                "DELETE FROM found_deals WHERE found_at < datetime('now', '-90 days')"
            )
            await db.execute(
                "DELETE FROM jev_cache WHERE created_at < datetime('now', '-30 days')"
            )
        except Exception:
            pass

        await db.commit()
    print(f"Database initialized at {db_path or DB_PATH}")

if __name__ == "__main__":
    asyncio.run(init_db())
