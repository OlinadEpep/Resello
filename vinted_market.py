from vinted_scraper import VintedScraper
import statistics
import logging

logger = logging.getLogger(__name__)

def get_vinted_market_price(query):
    scraper = VintedScraper("https://www.vinted.it")
    try:
        params = {"search_text": query}
        items = scraper.search(params)
        if not items:
            return None
        
        prices = []
        for item in items:
            try:
                p = float(item.price)
                prices.append(p)
            except (ValueError, TypeError):
                continue
        
        if not prices:
            return None
            
        # Remove outliers
        if len(prices) > 5:
            prices.sort()
            trim = max(1, int(len(prices) * 0.2)) # Trim more because Vinted has more noise
            prices = prices[trim:-trim]
            
        return round(statistics.median(prices), 2)
    except Exception as e:
        logger.error(f"Vinted market price error for '{query}': {e}")
        return None

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    q = "Nintendo 3DS"
    price = get_vinted_market_price(q)
    print(f"Vinted Market Price for '{q}': {price}€")
