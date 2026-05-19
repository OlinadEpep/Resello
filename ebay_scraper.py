import asyncio
from playwright.async_api import async_playwright
from playwright_stealth import Stealth
from bs4 import BeautifulSoup
import re
import statistics
import random

async def get_market_price_playwright(query):
    async with async_playwright() as p:
        # Try with a real-looking browser
        browser = await p.chromium.launch(headless=True)
        # Create a persistent-like context (or just more headers)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080}
        )
        page = await context.new_page()
        await Stealth().apply_stealth_async(page)
        
        try:
            # Step 1: Visit homepage to get cookies and look less like a bot
            await page.goto("https://www.ebay.it", wait_until="networkidle", timeout=60000)
            await asyncio.sleep(random.uniform(2, 4))
            
            # Step 2: Search for the item
            search_url = f"https://www.ebay.it/sch/i.html?_nkw={query.replace(' ', '+')}&LH_Sold=1&LH_Complete=1&_ipg=60"
            await page.goto(search_url, wait_until="networkidle", timeout=60000)
            await asyncio.sleep(random.uniform(2, 5))
            
            content = await page.content()
            
            if "Access Denied" in content or "Accedi" in await page.title():
                print("eBay still blocking or asking for login.")
                # Try to take a screenshot for debugging if possible
                # await page.screenshot(path="ebay_block.png")
                await browser.close()
                return None

            soup = BeautifulSoup(content, 'html.parser')
            price_tags = soup.find_all('span', class_='s-item__price')
            
            prices = []
            for tag in price_tags:
                price_text = tag.get_text()
                if ' a ' in price_text: continue
                match = re.search(r'(\d+[\.,]\d+)', price_text)
                if match:
                    price_val = match.group(1).replace('.', '').replace(',', '.')
                    try:
                        prices.append(float(price_val))
                    except ValueError: continue
            
            if not prices:
                print(f"No prices found for {query}")
                await browser.close()
                return None
            
            if len(prices) > 5:
                prices.sort()
                trim = max(1, int(len(prices) * 0.1))
                prices = prices[trim:-trim]
                
            median_price = statistics.median(prices)
            await browser.close()
            return round(median_price, 2)
            
        except Exception as e:
            print(f"Playwright eBay error: {e}")
            await browser.close()
            return None

if __name__ == "__main__":
    q = "Nintendo 3DS"
    print(f"Calculating market price for: {q}...")
    mp = asyncio.run(get_market_price_playwright(q))
    print(f"Market Price: {mp}")
