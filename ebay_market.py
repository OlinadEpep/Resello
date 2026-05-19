import requests
from bs4 import BeautifulSoup
import re
import statistics

def get_market_price(query):
    """
    Scrapes eBay sold listings to calculate the median market price.
    """
    # URL for sold items on eBay.it
    url = f"https://www.ebay.it/sch/i.html?_nkw={query.replace(' ', '+')}&LH_Sold=1&LH_Complete=1&_ipg=60"
    headers = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # eBay listing prices usually have class 's-item__price'
        # Sold listings might also have 's-item__trending-price' or others, but 's-item__price' is common
        # We need to filter out the "positive" prices (sold)
        price_tags = soup.find_all('span', class_='s-item__price')
        
        prices = []
        for tag in price_tags:
            price_text = tag.get_text()
            # Handle ranges like "EUR 10,00 a EUR 20,00" -> pick first or ignore
            if ' a ' in price_text:
                continue
                
            # Extract number using regex. eBay.it uses "EUR 150,00" or similar
            match = re.search(r'(\d+[\.,]\d+)', price_text)
            if match:
                price_val = match.group(1).replace('.', '').replace(',', '.')
                try:
                    prices.append(float(price_val))
                except ValueError:
                    continue
        
        if not prices:
            print(f"No prices found for {query}")
            return None
        
        # Remove top and bottom 10% to avoid outliers (shipping costs or accessories only)
        if len(prices) > 5:
            prices.sort()
            trim = max(1, int(len(prices) * 0.1))
            prices = prices[trim:-trim]
            
        median_price = statistics.median(prices)
        return round(median_price, 2)

    except Exception as e:
        print(f"Error scraping eBay market price for {query}: {e}")
        return None

if __name__ == "__main__":
    test_query = "Nintendo 3DS"
    price = get_market_price(test_query)
    print(f"Market Price for '{test_query}': {price}€")
