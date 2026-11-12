from vinted_scraper import VintedScraper
import json

def test_vinted():
    scraper = VintedScraper("https://www.vinted.it")
    try:
        params = {"search_text": "Nintendo 3DS"}
        items = scraper.search(params)
        if not items:
            print("No items found on Vinted.")
            return
        
        print(f"Found {len(items)} items on Vinted.")
        for item in items[:5]:
            print(f"- {item.title}: {item.price} {item.currency} ({item.url})")
            
    except Exception as e:
        print(f"Vinted error: {e}")

if __name__ == "__main__":
    test_vinted()
