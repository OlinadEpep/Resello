# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Telegram bot that monitors Vinted (vinted.it) for underpriced listings. For each tracked query it estimates a market price, then every 10 minutes searches for new listings whose **total price** (base + service fee + shipping) is below a threshold (default: market price × 0.7, or a user-set max via `/cerca oggetto | prezzo`), filters false positives, and notifies the chat. Code, comments, log messages and bot replies are in Italian.

## Commands

Always use the project venv (`./venv/bin/python3`); the systemd unit and `main.py` both run `bot.py` through it.

```bash
./venv/bin/pip install -r requirements.txt && ./venv/bin/playwright install chromium
./venv/bin/python3 init_db.py          # create/migrate deals.db schema (idempotent, also prunes old rows)
./venv/bin/python3 bot.py              # run the bot (needs TELEGRAM_TOKEN in .env)

./venv/bin/python3 -m pytest                                   # full suite (live tests skipped)
./venv/bin/python3 -m pytest tests/test_market.py              # one file
./venv/bin/python3 -m pytest tests/test_market.py::test_market_price_median_trimmed  # one test
LIVE=1 ./venv/bin/python3 -m pytest -m live                    # opt-in tests against real Vinted / TypeSafe APIs

./venv/bin/python3 scripts/tune_thresholds.py [--db deals.db] [--min-n 50]  # offline replay of jev_signals to propose JEV_THRESHOLDS_JSON
```

`pytest.ini` sets `asyncio_mode = auto`, so async tests need no decorator. There is no linter/formatter configured. Deployment is via `deal-hunter.service` (systemd, logs with `journalctl -u deal-hunter.service -f`).

## Architecture

**Async-only rule (from AGENTS.md):** the bot previously froze due to sync scraping and `asyncio.run()` inside the event loop. Everything in the bot path must be async: use `AsyncVintedScraper` (from the `vinted-scraper` library), never the sync `VintedScraper`, and never call `asyncio.run()` outside `init_db.py`'s `__main__`.

### Flow

- `bot.py` — Telegram handlers (`/cerca`, `/lista`, `/stop`, `/funzionamento`, ...) and the `job_queue.run_repeating(check_deals_callback, interval=600)` loop; `build_application()` wires everything. When adding a command, also add it to `BOT_COMMANDS` (the "/" menu) and `/help`.
  - `/cerca` reuses a market price for the same (query, chat) if younger than `MARKET_TTL_HOURS`, else calls `get_vinted_market_price`.
  - `check_deals_callback` first refreshes stale market prices (`_maybe_refresh_market`), then opens **one** shared `AsyncVintedScraper`, one `VintedPlaywrightScraper` (started once, closed in `finally`) and one `httpx.AsyncClient` for the whole cycle, and runs `_check_single_item` per tracked item.
  - `_check_single_item` pipeline: library search → Playwright fallback if empty → normalize → prefilter by base price + dedup against `found_deals` → optional Jev rerank (top-k) → parallel detail fetch (semaphore 4) to get real shipping/fees → total-price check → `is_valid_item` regex filter → Jev `judge()` (skip / review / notify) → `notify_deal`.
- `notifica.py` + `vinted_filters.py` — the `/notifica` feature, independent of the deal pipeline above: it notifies **every** new listing matching a saved Vinted search (category / brand / sizes, or a pasted `vinted.it/catalog?...` link), with no market price and no Jev.
  - `notifica.register(app)` adds a `ConversationHandler` (category → brand → sizes → confirm) in **handler group 1**, so group-0 commands keep working mid-setup and also end it (`_other_command` fallback). `context.user_data["notifica"]` holds the draft and doubles as the "setup in progress" flag.
  - A saved search is just a normalized URL in the `alerts` table (`order=newest_first` forced). `check_alerts_callback` (every `ALERT_INTERVAL_SECONDS`, default 120) fetches each distinct URL once with Playwright and parses the grid with `vinted_filters.parse_catalog_items`.
  - "New" is decided by `vinted_filters.detect_new_items`: id not in `alert_seen` **and** id above `watermark - ALERT_ID_MARGIN`. Vinted ids grow over time, so the watermark excludes old listings that re-enter page 1 (bumped, or shifted up from page 2 when others sell); do not replace it with a plain seen-set. The first pass (`seeded = 0`) only records what is there; an empty search seeds its watermark from the unfiltered newest feed.
  - Vinted metadata comes from endpoints that still answer through `AsyncVintedWrapper.curl`: `/api/v2/item_upload/catalogs` (category tree), `/api/v2/brands?keyword=`, `/api/v2/size_groups?catalog_ids=`.
- `vinted_market.py` — `get_vinted_market_price(query)` returns a **3-tuple `(price | None, dynamic_keywords, meta)`** (`_norm_market_res` normalizes legacy 2-tuples). Pipeline: 2-page fetch → regex/dynamic-keyword filtering → Jev "full product" filter → IQR `robust_median` with guardrails → `sanity_check_market` category bands; Playwright fallback when the library path fails. Each estimate is logged to the `market_estimates` table.
- `vinted_playwright.py` — `VintedPlaywrightScraper` (playwright-stealth, rotating UA/viewport). Preferred usage is `start()` / many calls / `close()`; legacy per-page open/close still exists.
- `utils.py` — central constants (service fee formula, `DEFAULT_SHIPPING`, hard/soft negative keywords, stop words, market guardrail constants, sanity bands), `is_valid_item`, `extract_dynamic_keywords` (derives negative keywords from suspiciously cheap listings' titles), `robust_median`, and HTML detail parsing. Keep price constants here rather than duplicating them in modules.
- `deal_judge.py` — optional semantic filter using the TypeSafe SDK (`typesafe_sdk`, model "Jev", via `system_one` calls with typed `Choice`/`Noul`/`Score` questions). Accessed through the `get_judge()` singleton, which returns `None` when `TYPESAFE_API_KEY` is unset or the SDK is missing. **Everything Jev-related is fail-open**: errors or absence fall back to regex-only filtering and never block a notification. Weights/thresholds live in `CONFIG` and can be overridden with `JEV_THRESHOLDS_JSON`. Verdicts are cached per URL hash in `jev_cache`; every judgment is logged to `jev_signals` (consumed by `scripts/tune_thresholds.py`). `.agents/skills/typesafe-ai/SKILL.md` documents the SDK.
- `init_db.py` — owns the SQLite schema (`items`, `found_deals`, `jev_cache`, `jev_signals`, `market_estimates`, `alerts`, `alert_seen`) with in-place migrations. Schema changes go here; `bot.py`'s `post_init` calls `init_db()` at every startup (and registers the Telegram command menu from `BOT_COMMANDS`), so a restart is enough to apply them. `bot.py` hardcodes `deals.db`; `deal_judge.py` reads `DEALS_DB_PATH` (default `deals.db`).

### Configuration (`.env`, see `.env.example`)

`TELEGRAM_TOKEN`, `TYPESAFE_API_KEY`, `TYPESAFE_MODEL`, `MARKET_TTL_HOURS`, `RERANK_ENABLED` / `RERANK_TOP_K` / `RERANK_MIN_SCORE`, `JEV_THRESHOLDS_JSON`, `ALERT_INTERVAL_SECONDS` / `ALERT_MAX_NOTIFY` / `ALERT_ID_MARGIN`.

### Tests

Tests are fully offline: they monkeypatch `AsyncVintedScraper`, the Playwright fallback, `_apply_jev_market_filter` and `typesafe_sdk.AsyncTypeSafeClient`, and use in-memory/tmp SQLite. Call `deal_judge.reset_judge_for_tests()` when a test depends on `get_judge()`'s singleton state. Anything that hits the network belongs in `tests/test_live.py` behind `LIVE=1`. `tests/test_notifica_flow.py` drives the real `/notifica` conversation through a PTB `Application` with a fake `BaseRequest`; use it as the pattern for testing handler wiring.
