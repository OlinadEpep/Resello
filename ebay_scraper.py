import asyncio
import logging
import random
import re
import statistics
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class eBayScraper:
    def __init__(self):
        self.base_url = "https://www.ebay.it"
        self.user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"

    async def _get_page_content(self, url):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(
                user_agent=self.user_agent,
                viewport={'width': 1920, 'height': 1080}
            )
            page = await context.new_page()
            # await Stealth().apply_stealth_async(page)
            
            try:
                # Basic anti-bot: visit home first
                await page.goto(self.base_url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(random.uniform(1, 3))
                
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                await asyncio.sleep(random.uniform(2, 4))
                
                content = await page.content()
                if "Access Denied" in content or "Accedi" in await page.title():
                    logger.error("eBay blocked the request or redirect to login.")
                    return None
                
                return content
            except Exception as e:
                logger.error(f"Error fetching eBay page: {e}")
                return None
            finally:
                await browser.close()

    def _parse_price(self, price_text):
        if ' a ' in price_text: return None
        match = re.search(r'(\d+[\.,]\d+)', price_text)
        if match:
            return float(match.group(1).replace('.', '').replace(',', '.'))
        return None

    async def get_market_price(self, query):
        url = f"{self.base_url}/sch/i.html?_nkw={query.replace(' ', '+')}&LH_Sold=1&LH_Complete=1&_ipg=60"
        content = await self._get_page_content(url)
        if not content: return None

        soup = BeautifulSoup(content, 'html.parser')
        price_tags = soup.find_all('span', class_='s-item__price')
        
        prices = []
        for tag in price_tags:
            p = self._parse_price(tag.get_text())
            if p: prices.append(p)
            
        if not prices:
            logger.warning(f"No sold prices found for: {query}")
            return None
        
        if len(prices) > 5:
            prices.sort()
            trim = max(1, int(len(prices) * 0.1))
            prices = prices[trim:-trim]
            
        return round(statistics.median(prices), 2)

    async def search_deals(self, query, max_price):
        url = f"{self.base_url}/sch/i.html?_nkw={query.replace(' ', '+')}&_udhi={max_price}&_ipg=60&_sop=10" # _sop=10 is "Newly Listed"
        content = await self._get_page_content(url)
        if not content: return []

        soup = BeautifulSoup(content, 'html.parser')
        items = soup.find_all('div', class_='s-item__info')
        
        found_deals = []
        for item in items:
            title_tag = item.find('div', class_='s-item__title')
            price_tag = item.find('span', class_='s-item__price')
            link_tag = item.find('a', class_='s-item__link')
            
            if not (title_tag and price_tag and link_tag): continue
            
            title = title_tag.get_text(strip=True)
            if "Risultati corrispondenti a meno parole" in title: continue # eBay's fuzzy match separator
            
            price = self._parse_price(price_tag.get_text())
            url = link_tag['href'].split('?')[0] # Clean URL
            
            if price and price <= max_price:
                found_deals.append({
                    'title': title,
                    'price': price,
                    'url': url,
                    'platform': 'eBay'
                })
        
        return found_deals

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = eBayScraper()
    q = "Nintendo 3DS"
    async def test():
        mp = await scraper.get_market_price(q)
        print(f"Market Price: {mp}")
        if mp:
            deals = await scraper.search_deals(q, mp * 0.7)
            print(f"Found {len(deals)} deals")
            for d in deals[:3]:
                print(f"- {d['title']}: {d['price']}€ ({d['url']})")
    
    asyncio.run(test())
