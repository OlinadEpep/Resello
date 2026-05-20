# Deal Hunter Bot

A Telegram bot that monitors Vinted and eBay for deals. It calculates market prices based on current and past listings and notifies users when items are found below a target price (defaulting to 30% below market value).

## Project Overview

- **Main Technologies:** Python, `python-telegram-bot`, SQLite, Playwright, BeautifulSoup, `vinted-scraper`.
- **Architecture:**
    - `bot.py`: Telegram interface, command handling, and job scheduling (apscheduler).
    - `ebay_scraper.py`: Playwright-based scraper for eBay listings and sold items.
    - `vinted_market.py`: Vinted price estimation using the `vinted-scraper` library.
    - `init_db.py`: Database schema initialization.
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
    python3 init_db.py
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
    - eBay scraping uses Playwright in headless mode with stealth tactics to avoid detection.
    - Vinted scraping relies on the `vinted-scraper` library.
- **Scheduling:** The bot checks for new deals every 10 minutes (defined in `bot.py`).
- **Market Price:** Calculated using the median price of results, with outliers trimmed (top/bottom 10-20%).

## Bot Commands
- `/start`: Welcome message.
- `/help`: Guide on how to use the bot.
- `/cerca [oggetto]`: Start monitoring an item (auto-calculates target price).
- `/cerca [oggetto] | [prezzo]`: Start monitoring with a custom target price.
- `/lista`: List currently monitored items.
- `/stop [oggetto]`: Stop monitoring an item.
