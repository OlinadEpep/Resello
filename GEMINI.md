# Deal Hunter Bot

A Telegram bot that monitors Vinted for deals. It calculates market prices based on current and past listings and notifies users when items are found below a target price (defaulting to 30% below market value).

## Project Overview

- **Main Technologies:** Python, `python-telegram-bot`, SQLite, Playwright, BeautifulSoup, `vinted-scraper`.
- **Architecture:**
    - `deal_hunter/bot/app.py` (launched by the root `bot.py`): Telegram interface, command handling, and job scheduling (apscheduler).
    - `deal_hunter/vinted/market.py`: Vinted price estimation using the `vinted-scraper` library.
    - `deal_hunter/core/db.py`: Database schema initialization.
    - `main.py`: Application entry point.
    - `deals.db`: SQLite database for tracking items and preventing duplicate notifications.

## Building and Running

### Prerequisites
- Python 3.10+
- Virtual environment (`venv`)
- Playwright browsers

### Setup
1.  **Environment Variables:** Create a `.env` file based on `.env.example`:
    ```env
    TELEGRAM_TOKEN=your_bot_token_here
    ```
2.  **Installation:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    playwright install chromium
    ```
3.  **Database Initialization:**
    ```bash
    python3 -m deal_hunter.core.db
    ```

### Running the Bot
- **Manual:**
    ```bash
    python3 main.py
    ```
- **Systemd Service:** Use `deal-hunter.service` to deploy as a background service.

## Development Conventions

- **Database:** Uses SQLite for persistence. The `items` table tracks monitored queries, and `found_deals` tracks already notified URLs to avoid spam.
- **Scraping:**
    - Vinted scraping relies on the `vinted-scraper` library and Playwright fallback.
- **Scheduling:** The bot checks for new deals every 10 minutes (defined in `deal_hunter/bot/app.py`).
- **Market Price:** Calculated using the median price of results, with outliers trimmed (top/bottom 10-20%).

## Bot Commands
- `/start`: Welcome message.
- `/help`: Guide on how to use the bot.
- `/cerca [oggetto]`: Start monitoring an item (auto-calculates target price).
- `/cerca [oggetto] | [prezzo]`: Start monitoring with a custom target price.
- `/lista`: List currently monitored items.
- `/stop [oggetto]`: Stop monitoring an item.
