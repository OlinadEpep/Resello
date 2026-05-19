import sqlite3
import os

DB_PATH = "deals.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Table for tracked items
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        chat_id INTEGER NOT NULL,
        market_price REAL,
        max_price REAL,
        last_market_update DATETIME,
        is_active INTEGER DEFAULT 1,
        UNIQUE(name, chat_id)
    )
    ''')

    # Table for found deals to prevent duplicates
    cursor.execute('''
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

    conn.commit()
    conn.close()
    print(f"Database initialized at {DB_PATH}")

if __name__ == "__main__":
    init_db()
