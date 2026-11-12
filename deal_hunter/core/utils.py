import re
import collections
import statistics

# Fattore per calcolare il prezzo minimo accettabile (40% del prezzo stimato)
MIN_PRICE_FACTOR = 0.4

# Costanti centralizzate per il calcolo del prezzo totale Vinted.
# Usate in vinted_market.py, vinted_playwright.py e bot.py per evitare
# valori hardcoded divergenti.
SERVICE_FEE_BASE = 0.70
SERVICE_FEE_RATE = 0.05
DEFAULT_SHIPPING = 3.50
DETAIL_FETCH_TIMEOUT = 10.0

# Header HTTP condiviso per il fetch veloce delle pagine annuncio.
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
}


def compute_service_fee(base_price):
    """Stima la Protezione Acquisti Vinted: 0.70€ + 5% del prezzo base."""
    try:
        return round(SERVICE_FEE_BASE + SERVICE_FEE_RATE * float(base_price), 2)
    except (TypeError, ValueError):
        return 0.0


def build_total_price(base_price, shipping_price=None, service_fee=None):
    """
    Costruisce il prezzo totale (base + protezione + spedizione).
    shipping None -> DEFAULT_SHIPPING, service_fee None -> stima da base.
    Ritorna None se base_price non è valido.
    """
    try:
        base = float(base_price)
    except (TypeError, ValueError):
        return None
    ship = DEFAULT_SHIPPING if shipping_price is None else float(shipping_price)
    fee = compute_service_fee(base) if service_fee is None else float(service_fee)
    return round(base + fee + ship, 2)

# Parole negative fisse che si applicano a quasi tutto.
# Fase 1: estese oltre rotture/ricambi agli accessori più comuni che
# inquinano la mediana (controller, giochi, cover, audio, supporti...).
# Nota: 'scatola'/'box'/'manuale' volutamente NON qui: "console con scatola"
# è un full product; quei casi li decide Jev + dynamic keywords.
# HARD = sempre scartate. SOFT = accessori, bypassate se il titolo contiene
# un hint full-product (bundle "console + controller" resta, decide Jev).
HARD_NEGATIVE_KEYWORDS = [
    'ricambio', 'ricambi', 'pezzi', 'rotto', 'rotta', 'rotte', 'broken', 'parts',
    'only', 'non funzionante', 'not working',
]
SOFT_NEGATIVE_KEYWORDS = [
    'controller', 'controllers', 'dualsense', 'dualshock', 'joystick', 'joypad',
    'gamepad', 'joycon', 'joy-con',
    'cover', 'custodia', 'custodie', 'case', 'guscio',
    'gioco', 'giochi', 'game', 'games', 'videogioco',
    'cuffie', 'headset', 'auricolari', 'microfono',
    'cavo', 'cavi', 'hdmi', 'cinturino', 'caricatore', 'charger',
    'supporto', 'stazione', 'dock', 'holder',
    'skin', 'sticker', 'adesivo', 'adesiva', 'pellicola',
    'volante', 'visore', 'telecomando',
]
# Compat: vecchio nome = unione (test esistenti usano is_valid_item base).
BASE_NEGATIVE_KEYWORDS = HARD_NEGATIVE_KEYWORDS + SOFT_NEGATIVE_KEYWORDS

# Hint full-product: se presenti, le SOFT non scartano (bundle console+extra).
# 'base' volutamente escluso: troppo ambiguo ("base d'appoggio" vs "base price").
FULL_PRODUCT_HINTS = [
    'console', 'consolle', 'slim', 'fat', 'pro', 'edition', 'edizione',
    'disco', 'disc', 'digitale', 'digital', 'bundle', 'lettore',
]

# STOP_WORDS per dynamic keywords: rimossi i sostantivi mercato-rilevanti
# ('scatola','box','manual','manuale') che prima non potevano mai emergere
# come negative keywords — bug che lasciava "scatola vuota PS5" nel calcolo.
MARKET_EXCLUDE_FROM_STOP = {'scatola', 'box', 'manual', 'manuale'}

# Lista di stop words in Italiano e Inglese, più termini comuni di vendita
STOP_WORDS = set([
    # Italiano
    'il', 'lo', 'la', 'i', 'gli', 'le', 'un', 'uno', 'una', 'di', 'a', 'da', 'in', 'con', 'su', 'per', 'tra', 'fra', 'e', 'o', 'ma', 'se', 'non', 'che', 'cui', 'come', 'al', 'allo', 'alla', 'ai', 'agli', 'alle', 'del', 'dello', 'della', 'dei', 'degli', 'delle', 'dal', 'dallo', 'dalla', 'dai', 'dagli', 'dalle', 'nel', 'nello', 'nella', 'nei', 'negli', 'nelle', 'sul', 'sullo', 'sulla', 'sui', 'sugli', 'sulle',
    # Inglese
    'the', 'a', 'an', 'and', 'or', 'but', 'if', 'of', 'at', 'by', 'for', 'with', 'about', 'against', 'between', 'into', 'through', 'during', 'before', 'after', 'above', 'below', 'to', 'from', 'up', 'down', 'in', 'out', 'on', 'off', 'over', 'under', 'again', 'further', 'then', 'once',
    # Termini di vendita/condizioni
    'nuovo', 'usato', 'scatola', 'spedizione', 'vendo', 'affare', 'originale', 'perfetto', 'condizioni', 'ottimo', 'buono', 'senza', 'con', 'euro', 'eu', 'prezzo', 'scambio', 'regalo', 'new', 'used', 'mint', 'condition', 'shipping', 'free', 'postage', 'sale', 'sell', 'box', 'manual', 'manuale'
])

def extract_dynamic_keywords(items_data, query):
    """
    Analizza statisticamente i titoli per trovare parole ricorrenti in oggetti "troppo economici".
    items_data: lista di dict [{'title': '...', 'price': ...}]
    query: la stringa di ricerca originale
    """
    if not items_data:
        return []
    
    prices = [item['price'] for item in items_data]
    if not prices: return []
    
    median_price = statistics.median(prices)
    
    # Identifica il secchio degli oggetti "economici" (es. < 40% della mediana)
    cheap_items = [item for item in items_data if item['price'] < median_price * 0.4]
    normal_items = [item for item in items_data if item['price'] >= median_price * 0.4]
    
    if not cheap_items:
        return []
        
    query_words = set(re.findall(r'\w+', query.lower()))

    market_stop = STOP_WORDS - MARKET_EXCLUDE_FROM_STOP

    def tokenize(title):
        # Estrae parole alfanumeriche di almeno 3 caratteri
        words = re.findall(r'\b[a-z0-9]{3,}\b', title.lower())
        return [w for w in words if w not in market_stop and w not in query_words]

    cheap_words_counter = collections.Counter()
    for item in cheap_items:
        for w in tokenize(item['title']):
            cheap_words_counter[w] += 1
            
    normal_words_counter = collections.Counter()
    for item in normal_items:
        for w in tokenize(item['title']):
            normal_words_counter[w] += 1

    dynamic_kws = []
    # Se una parola è frequente negli oggetti economici e rara/assente in quelli normali, è una parola negativa
    cheap_threshold = max(2, len(cheap_items) * 0.2) # deve apparire in almeno il 20% degli oggetti economici
    for word, count in cheap_words_counter.items():
        if count >= cheap_threshold:
            # Se appare raramente negli oggetti normali (meno del 5%)
            if normal_words_counter[word] <= max(1, len(normal_items) * 0.05):
                dynamic_kws.append(word)
                
    return dynamic_kws

def calculate_min_price(market_price, max_price):
    """
    Calcola la soglia minima di prezzo sotto la quale l'articolo è considerato un falso positivo.
    Usa il 30% del prezzo di mercato se disponibile, altrimenti il 30% del prezzo massimo.
    """
    reference = market_price if (market_price and market_price > 0) else max_price
    if reference:
        return round(reference * MIN_PRICE_FACTOR, 2)
    return 0.0

def is_valid_item(title, price, dynamic_keywords=None, for_market_calc=False, min_price=None):
    """
    Filtra i falsi positivi basandosi su parole chiave nel titolo 
    e una soglia minima di prezzo.
    """
    if not title or price is None:
        return False
        
    if for_market_calc and price < 15.0:
        return False
        
    if min_price is not None and price < min_price:
        return False
        
    t = title.lower()

    def _has(kw):
        return re.search(r'\b' + re.escape(kw) + r'\b', t) is not None

    for kw in HARD_NEGATIVE_KEYWORDS:
        if _has(kw):
            return False

    # Bundle full-product ("console + controller", "console + giochi"):
    # le SOFT non decidono, passa a Jev/dynamic.
    has_full_hint = any(_has(h) for h in FULL_PRODUCT_HINTS)
    if not has_full_hint:
        for kw in SOFT_NEGATIVE_KEYWORDS:
            if _has(kw):
                return False

    if dynamic_keywords:
        for kw in dynamic_keywords:
            if _has(kw):
                # Dynamic sempre attive, ma non su bundle full-product
                # se la keyword è generica da STOP (es. 'con'): richiede hint assente.
                if has_full_hint and kw in ('con', 'nuovo', 'usato', 'originale'):
                    continue
                return False

    return True


# ---- Fase 0+1+3: statistica robusta, guardrail e bande di sanità ----

MIN_FULL_SAMPLE = 5          # sotto: stima non affidabile -> None (guardrail)
MAX_DISPERSION_IQR = 0.6     # IQR/mediana sopra: distribuzione bimodale -> None
MIN_KEEP_SHARE = 0.2         # se resta <20% del grezzo: probabile query generica -> None

# Bande di sanità totale (base+fee+sped) per hint di categoria.
# Fuori banda -> scarta stima e suggerisce query più specifica.
CATEGORY_SANITY_BANDS = [
    (re.compile(r'playstation\s*5|\bps5\b', re.I), 200.0, 550.0, 'playstation 5 console'),
    (re.compile(r'playstation\s*4|\bps4\b', re.I), 80.0, 300.0, 'playstation 4 console'),
    (re.compile(r'nintendo\s*switch', re.I), 120.0, 400.0, 'nintendo switch console'),
    (re.compile(r'nintendo\s*3ds', re.I), 50.0, 220.0, 'nintendo 3ds console funzionante'),
    (re.compile(r'xbox\s*series\s*x', re.I), 200.0, 550.0, 'xbox series x console'),
    (re.compile(r'xbox\s*series\s*s', re.I), 120.0, 350.0, 'xbox series s console'),
]


def sanity_check_market(query, price):
    """Ritorna (ok, reason, suggested_query). Fuori banda -> ok=False."""
    if price is None:
        return False, 'no_price', None
    q = query or ''
    # Query gioco (contiene titolo gioco + piattaforma): le bande console
    # non applicano — un gioco PS5 a 40€ è plausibile.
    if re.search(r'gioco|game|death stranding|\bfc\s?\d|\bfifa\b|\bgta\b|spider|god of war|elden|zelda|mario|pok[eé]mon|spiderman|horizon|call of duty|\bcod\b', q, re.I):
        return True, 'game_query_skipped', None
    for rx, lo, hi, suggestion in CATEGORY_SANITY_BANDS:
        if rx.search(query or ''):
            if lo <= price <= hi:
                return True, 'in_band', None
            return False, f'out_of_band_{lo:g}_{hi:g}', suggestion
    return True, 'no_band', None


def robust_median(prices):
    """Mediana con IQR outlier-rejection asimmetrica + guardrail.

    Ritorna (median_or_None, meta dict). Mai eccezioni: fail-open con reason.
    """
    meta = {'n': len(prices) if prices else 0}
    if not prices:
        return None, {**meta, 'reason': 'empty'}
    vals = sorted(float(p) for p in prices)
    n = len(vals)
    med = statistics.median(vals)
    meta['median_raw'] = round(med, 2)
    if n < MIN_FULL_SAMPLE:
        return None, {**meta, 'reason': f'too_few_n={n}'}
    # IQR
    q1 = statistics.median(vals[:n // 2])
    q2 = statistics.median(vals[(n + 1) // 2:])
    iqr = q2 - q1
    meta.update({'q1': round(q1, 2), 'q3': round(q2, 2), 'iqr': round(iqr, 2)})
    dispersion = (iqr / med) if med else 999.0
    meta['dispersion'] = round(dispersion, 3)
    if dispersion > MAX_DISPERSION_IQR:
        return None, {**meta, 'reason': f'high_dispersion_{dispersion:.2f}'}
    # Fence asimmetrico: più severo sulla coda cheap (1.0*IQR sotto,
    # 1.5*IQR sopra) perché il rumore Vinted è quasi tutto cheap.
    lo_fence = q1 - 1.0 * iqr
    hi_fence = q2 + 1.5 * iqr
    kept = [v for v in vals if lo_fence <= v <= hi_fence]
    meta['n_kept'] = len(kept)
    meta['keep_share'] = round(len(kept) / n, 3) if n else 0.0
    if len(kept) < MIN_FULL_SAMPLE or meta['keep_share'] < MIN_KEEP_SHARE:
        return None, {**meta, 'reason': 'too_few_after_iqr'}
    return round(statistics.median(kept), 2), {**meta, 'reason': 'ok'}

def parse_item_details_from_html(html_content):
    """
    Parses base price, shipping price, and service fee (buyer protection)
    from the Vinted item detail HTML content.
    """
    from bs4 import BeautifulSoup
    import re
    
    prices = {
        'base_price': None,
        'shipping_price': 0.0,
        'service_fee': 0.0,
        'total_price': None
    }
    
    # Pre-clean: replace escaped quotes in Next.js streamed payload
    cleaned_html = html_content.replace('\\"', '"')
    
    # 1. Base price
    base_match = re.search(r'"originalAskingAmount"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', cleaned_html)
    if base_match:
        try:
            prices['base_price'] = float(base_match.group(1))
        except ValueError:
            pass
            
    # 2. Shipping Details
    shipping_match = re.search(r'"shippingDetails"\s*:\s*\{[^}]*?"price"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', cleaned_html)
    if shipping_match:
        try:
            prices['shipping_price'] = float(shipping_match.group(1))
        except ValueError:
            pass
    else:
        if '"isFreeShipping":true' in cleaned_html:
            prices['shipping_price'] = 0.0
            
    # 3. Service Fee (Buyer Protection)
    bp_match = re.search(r'"buyerProtection"\s*:\s*\{[^}]*?"finalPrice"\s*:\s*\{\s*"amount"\s*:\s*"([^"]+)"', cleaned_html)
    if bp_match:
        try:
            prices['service_fee'] = float(bp_match.group(1))
        except ValueError:
            pass
            
    # Fallbacks if base_price was not found in RSC JSON
    if prices['base_price'] is None:
        soup = BeautifulSoup(html_content, 'html.parser')
        price_tag = soup.select_one('[data-testid*="price"]')
        if price_tag:
            m = re.search(r'(\d+[\.,]\d+)', price_tag.get_text())
            if m:
                prices['base_price'] = float(m.group(1).replace(',', '.'))
        
        if prices['base_price'] is None:
            for tag in soup.find_all(['h1', 'h2', 'h3']):
                text = tag.get_text().strip()
                if re.match(r'^\d+[\.,]\d+\s*€$', text):
                    prices['base_price'] = float(text.replace('€', '').replace(',', '.').strip())
                    break

    # If we have base_price, ensure service fee is computed
    if prices['base_price'] is not None and prices['service_fee'] == 0.0:
        prices['service_fee'] = compute_service_fee(prices['base_price'])

    # Compute total price
    if prices['base_price'] is not None:
        prices['total_price'] = round(prices['base_price'] + prices['service_fee'] + prices['shipping_price'], 2)

    return prices

async def fetch_item_details(url, playwright_scraper=None, http_client=None):
    """
    Fetches the item page and extracts its price components.
    Uses fast async httpx first (condivisibile fra chiamate parallele),
    falls back to Playwright if needed.
    Se http_client è fornito viene riusato (connection pooling);
    altrimenti ne crea uno temporaneo e lo chiude.
    """
    import httpx
    import logging

    logger = logging.getLogger(__name__)

    async def fetch_fast(client):
        try:
            r = await client.get(url, headers=DEFAULT_HEADERS, timeout=DETAIL_FETCH_TIMEOUT)
            if r.status_code == 200 and r.text:
                return r.text
            logger.warning(f"Fast fetch {r.status_code} for {url}")
        except Exception as e:
            logger.warning(f"Error fast-fetching {url}: {e}")
        return None

    html_content = None
    if http_client is not None:
        html_content = await fetch_fast(http_client)
    else:
        import httpx as _httpx
        async with _httpx.AsyncClient(follow_redirects=True) as tmp_client:
            html_content = await fetch_fast(tmp_client)

    if not html_content and playwright_scraper:
        logger.info(f"Fallback to Playwright for fetching item page: {url}")
        try:
            html_content = await playwright_scraper.get_page_content(url)
        except Exception as e:
            logger.error(f"Error fetching URL {url} with Playwright: {e}")

    if not html_content:
        return None

    return parse_item_details_from_html(html_content)

