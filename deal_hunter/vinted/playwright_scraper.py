import asyncio
import logging
import random
import re
import statistics
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from deal_hunter.core.utils import is_valid_item, compute_service_fee, DEFAULT_SHIPPING

logger = logging.getLogger(__name__)

STATIC_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class VintedPlaywrightScraper:
    """Scraper Playwright con browser riusabile per tutto un ciclo di check.

    Uso consigliato (nuovo):
        vp = VintedPlaywrightScraper()
        await vp.start()
        try:
            ... await vp.search_deals(...) / get_market_price(...) ...
        finally:
            await vp.close()

    Uso legacy (una pagina alla volta, apre/chiude da solo):
        await vp.get_page_content(url)  # oppure _get_page_content(url)
    """

    def __init__(self):
        self.base_url = "https://www.vinted.it"
        try:
            self.ua = UserAgent()
        except Exception:
            self.ua = None
        self._playwright = None
        self._browser = None

    def _pick_ua(self):
        if self.ua:
            try:
                return self.ua.random
            except Exception:
                pass
        return STATIC_UA

    async def start(self):
        """Avvia Playwright + browser persistente (idempotente)."""
        if self._browser:
            return
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=True, args=["--disable-dev-shm-usage"]
        )

    async def close(self):
        """Chiude browser + playwright (sicuro anche se mai avviati)."""
        try:
            if self._browser:
                await self._browser.close()
        except Exception as e:
            logger.warning(f"Error closing Playwright browser: {e}")
        finally:
            self._browser = None
        try:
            if self._playwright:
                await self._playwright.stop()
        except Exception as e:
            logger.warning(f"Error stopping Playwright: {e}")
        finally:
            self._playwright = None

    async def _fetch_with_context(self, url, context):
        page = await context.new_page()
        try:
            await Stealth().apply_stealth_async(page)
            await page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(random.uniform(0.5, 1.5))
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # Wait for items to load (fix: indentazione corretta)
            try:
                await page.wait_for_selector('[data-testid*="grid-item"]', timeout=8000)
            except Exception:
                await asyncio.sleep(2)

            content = await page.content()
            title = await page.title()

            lowered = content.lower()
            if (
                "Attention Required! | Cloudflare" in title
                or "cloudflare-challenge" in lowered
                or "cf-challenge" in lowered
            ):
                logger.error("Vinted blocked the request (Cloudflare).")
                return None
            return content
        except Exception as e:
            logger.error(f"Error fetching Vinted page: {e}")
            return None
        finally:
            try:
                await page.close()
            except Exception:
                pass

    async def get_page_content(self, url):
        """Fetch di una pagina con browser riusato se avviato, altrimenti one-shot."""
        if self._browser:
            context = await self._browser.new_context(
                user_agent=self._pick_ua(),
                viewport={'width': 1920, 'height': 1080},
                device_scale_factor=1,
                is_mobile=False,
                has_touch=False,
                locale="it-IT",
                timezone_id="Europe/Rome",
            )
            try:
                return await self._fetch_with_context(url, context)
            finally:
                try:
                    await context.close()
                except Exception:
                    pass
        # Fallback one-shot (legacy): apre e chiude tutto
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            try:
                context = await browser.new_context(
                    user_agent=STATIC_UA,
                    viewport={'width': 1920, 'height': 1080},
                    device_scale_factor=1,
                    is_mobile=False,
                    has_touch=False,
                    locale="it-IT",
                    timezone_id="Europe/Rome",
                )
                try:
                    return await self._fetch_with_context(url, context)
                finally:
                    try:
                        await context.close()
                    except Exception:
                        pass
            finally:
                await browser.close()

    # Alias legacy: utils.fetch_item_details e vecchi test usavano _get_page_content
    async def _get_page_content(self, url):
        return await self.get_page_content(url)

    def _parse_price(self, price_text):
        # Vinted prices often look like "10,00 €"
        if not price_text:
            return None
        match = re.search(r'(\d+[\.,]\d+)', price_text)
        if match:
            try:
                return float(match.group(1).replace(',', '.'))
            except ValueError:
                return None
        return None

    async def get_market_price(self, query):
        from deal_hunter.core.utils import extract_dynamic_keywords, robust_median, sanity_check_market
        url = f"{self.base_url}/catalog?search_text={query.replace(' ', '+')}&order=relevance"
        content = await self.get_page_content(url)
        if not content:
            return None, [], {'reason': 'no_content'}

        soup = BeautifulSoup(content, 'html.parser')
        items = soup.select('[data-testid*="grid-item"]')

        items_data = []
        for item in items:
            title_tag = item.select_one('[data-testid*="title"]')
            price_tag = item.select_one('[data-testid*="price"]')

            if not title_tag or not price_tag:
                continue

            title = title_tag.get_text(strip=True)
            p = self._parse_price(price_tag.get_text())
            if p:
                total_p = p + compute_service_fee(p) + DEFAULT_SHIPPING
                items_data.append({'title': title, 'price': total_p})

        meta = {'n_raw': len(items_data)}
        if not items_data:
            logger.warning(f"No prices found on Vinted for: {query}")
            return None, [], {**meta, 'reason': 'empty'}

        import statistics as _stats
        meta['median_raw'] = round(_stats.median([d['price'] for d in items_data]), 2)

        # Estrai parole negative dinamiche
        dynamic_kws = extract_dynamic_keywords(items_data, query)

        # Filtra usando le parole negative trovate
        valid_prices = [
            item['price'] for item in items_data
            if is_valid_item(item['title'], item['price'], dynamic_keywords=dynamic_kws, for_market_calc=True)
        ]
        meta['n_regex_kept'] = len(valid_prices)

        if not valid_prices:
            logger.warning(f"No valid prices after dynamic filtering on Vinted for: {query}")
            return None, dynamic_kws, {**meta, 'reason': 'all_filtered_regex'}

        # Fase 0+1: IQR robusto con guardrail (niente più trim simmetrico cieco)
        price, robust_meta = robust_median(valid_prices)
        meta = {**meta, **{f'robust_{k}': v for k, v in robust_meta.items()}}
        if price is None:
            logger.warning(f"Playwright market '{query}' scartato: {robust_meta.get('reason')}")
            return None, dynamic_kws, meta

        ok, reason, suggestion = sanity_check_market(query, price)
        meta['sanity'] = reason
        if not ok:
            return None, dynamic_kws, {**meta, 'suggested_query': suggestion}

        return round(price, 2), dynamic_kws, meta

    async def search_deals(self, query, max_price, min_price=None, dynamic_keywords=None):
        url = f"{self.base_url}/catalog?search_text={query.replace(' ', '+')}&order=newest_first"
        content = await self.get_page_content(url)
        if not content:
            return []

        soup = BeautifulSoup(content, 'html.parser')
        items = soup.select('[data-testid*="grid-item"]')

        found_deals = []
        try:
            max_f = float(max_price)
        except (TypeError, ValueError):
            return []
        try:
            min_f = float(min_price) if min_price is not None else None
        except (TypeError, ValueError):
            min_f = None

        for item in items:
            title_tag = item.select_one('[data-testid*="title"]')
            price_tag = item.select_one('[data-testid*="price"]')
            link_tag = item.select_one('a[href*="/items/"]')

            if not (title_tag and price_tag and link_tag):
                continue

            title = title_tag.get_text(strip=True)
            price = self._parse_price(price_tag.get_text())
            item_url = link_tag['href']
            if not item_url.startswith('http'):
                item_url = self.base_url + item_url

            if price is None or price > max_f:
                continue
            if min_f is not None and price < min_f:
                continue
            if is_valid_item(title, price, dynamic_keywords=dynamic_keywords, for_market_calc=False, min_price=None):
                found_deals.append({
                    'title': title,
                    'price': price,
                    'url': item_url,
                    'platform': 'Vinted'
                })

        return found_deals


if __name__ == "__main__":
    import asyncio as _asyncio
    logging.basicConfig(level=logging.INFO)
    scraper = VintedPlaywrightScraper()
    q = "Nintendo 3DS"

    async def test():
        await scraper.start()
        try:
            mp, kws, meta = await scraper.get_market_price(q)
            print(f"Market Price: {mp}")
            print(f"Negative Keywords: {kws}")
            print(f"Meta: {meta}")
            if mp:
                deals = await scraper.search_deals(q, mp * 0.7, dynamic_keywords=kws)
                print(f"Found {len(deals)} deals")
                for d in deals[:3]:
                    print(f"- {d['title']}: {d['price']}€ ({d['url']})")
        finally:
            await scraper.close()

    _asyncio.run(test())
