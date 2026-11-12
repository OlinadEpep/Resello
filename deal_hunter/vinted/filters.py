"""Filtri di ricerca Vinted per le notifiche "nuovo annuncio" (/notifica).

Qui vive tutto ciò che serve a trasformare delle preferenze (categoria,
brand, taglie, oppure il nome esatto di un prodotto) in un link di ricerca
Vinted con i filtri attivi, e a leggere gli annunci dalla pagina dei
risultati:

- metadati Vinted (albero categorie, brand, taglie) via le API pubbliche
  che rispondono ancora con `AsyncVintedWrapper.curl`;
- nome esatto di un prodotto: `product_terms` / `matches_product` (la
  ricerca testuale di Vinted è larga, il titolo va ricontrollato qui);
- costruzione/normalizzazione del link di ricerca;
- parsing della griglia annunci della pagina `/catalog`;
- regola "è un annuncio nuovo?" (`detect_new_items`).

Nessuna dipendenza da Telegram o dal DB: tutto testabile offline.
"""

import logging
import re
import time
import unicodedata
from urllib.parse import parse_qsl, quote, urlsplit

from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

BASE_URL = "https://www.vinted.it"

CATALOG_TREE_ENDPOINT = "/api/v2/item_upload/catalogs"
BRANDS_ENDPOINT = "/api/v2/brands"
SIZE_GROUPS_ENDPOINT = "/api/v2/size_groups"

CATALOG_TREE_TTL_SECONDS = 24 * 3600

# Parametri del link che non sono filtri: vengono tolti quando l'utente
# incolla un link (l'ordinamento lo forziamo noi a "più recenti").
_NON_FILTER_PARAMS = {
    "order", "page", "per_page", "time", "search_id", "referrer",
    "disabled_personalization", "search_by_image_uuid",
}

_VINTED_HOST_RE = re.compile(r"^(?:www\.)?vinted\.[a-z]{2,3}(?:\.[a-z]{2})?$")

_catalog_cache = {"flat": None, "loaded_at": 0.0}


# --------------------------------------------------------------------------
# Metadati Vinted (rete)
# --------------------------------------------------------------------------

async def _curl(endpoint, params=None):
    """GET su un endpoint API Vinted con sessione/cookie gestiti dalla libreria."""
    from vinted_scraper import AsyncVintedWrapper

    wrapper = await AsyncVintedWrapper.create(BASE_URL)
    async with wrapper:
        return await wrapper.curl(endpoint, params)


def flatten_catalogs(tree):
    """Albero categorie Vinted -> lista piatta [{'id','title','path'}].

    `path` è la lista dei titoli dalla radice al nodo incluso, es.
    ['Uomo', 'Vestiti', 'Jeans', 'Jeans skinny'].
    """
    flat = []

    def walk(node, parents):
        title = (node.get("title") or "").strip()
        if node.get("id") is None or not title:
            return
        path = parents + [title]
        flat.append({"id": int(node["id"]), "title": title, "path": path})
        for child in node.get("catalogs") or []:
            walk(child, path)

    for root in tree or []:
        walk(root, [])
    return flat


async def load_catalog_tree(force=False):
    """Lista piatta delle categorie Vinted, in cache per 24h."""
    now = time.monotonic()
    cached = _catalog_cache["flat"]
    if cached and not force and now - _catalog_cache["loaded_at"] < CATALOG_TREE_TTL_SECONDS:
        return cached
    try:
        data = await _curl(CATALOG_TREE_ENDPOINT)
        flat = flatten_catalogs(data.get("catalogs"))
    except Exception as e:
        if cached:
            logger.warning(f"Aggiornamento categorie Vinted fallito, uso la cache: {e}")
            return cached
        raise
    if not flat:
        if cached:
            return cached
        raise RuntimeError("Vinted non ha restituito nessuna categoria")
    _catalog_cache["flat"] = flat
    _catalog_cache["loaded_at"] = now
    return flat


def category_label(cat):
    """Etichetta leggibile: 'Uomo › Vestiti › Jeans › Jeans skinny'."""
    return " › ".join(cat["path"])


_STOP_TOKENS = {
    "a", "da", "di", "e", "per", "con", "in", "il", "la", "le", "i", "gli",
    "lo", "un", "una", "del", "della", "dei", "the", "and", "of",
}


def _normalize(text):
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# Parole d'uso comune che nelle categorie Vinted hanno un altro nome.
_SYNONYMS = {"sneakers": "ginnastica", "sneaker": "ginnastica"}


def _tokens(text):
    return [_SYNONYMS.get(t, t) for t in _normalize(text).split() if t not in _STOP_TOKENS]


def _fuzzy(a, b):
    """Match tollerante a singolare/plurale: 'felpa'~'felpe', 'jean'~'jeans'."""
    if len(a) < 4 or len(b) < 4:
        return False
    return b.startswith(a[:-1]) or a.startswith(b[:-1])


def search_categories(flat, text, limit=8):
    """Cerca nell'albero categorie le voci più vicine alle parole dell'utente.

    Ogni parola vale di più se sta nel nome della categoria (3 esatta,
    2 simile) che nel percorso dei genitori (1), così "jeans skinny uomo"
    trova "Uomo › Vestiti › Jeans › Jeans skinny". Serve almeno una parola
    nel nome della categoria.
    """
    query = _tokens(text)
    if not query:
        return []
    scored = []
    for cat in flat:
        title_tokens = _tokens(cat["title"])
        parent_tokens = [t for part in cat["path"][:-1] for t in _tokens(part)]
        score = matched = in_title = 0
        for q in query:
            if q in title_tokens:
                pts, title_hit = 3, True
            elif any(_fuzzy(q, t) for t in title_tokens):
                pts, title_hit = 2, True
            elif q in parent_tokens or any(_fuzzy(q, t) for t in parent_tokens):
                pts, title_hit = 1, False
            else:
                continue
            score += pts
            matched += 1
            in_title += title_hit
        if not in_title:
            continue
        # A parità: nome più corto (più vicino a ciò che è stato scritto),
        # poi categoria più generale.
        scored.append((-matched, -score, len(title_tokens), len(cat["path"]), cat["id"], cat))
    scored.sort(key=lambda s: s[:5])
    return [s[5] for s in scored[:limit]]


# --------------------------------------------------------------------------
# Nome esatto di un prodotto
# --------------------------------------------------------------------------

PRODUCT_TEXT_MAX_LENGTH = 60


def clean_product_text(text):
    """Nome prodotto come l'ha scritto l'utente, senza spazi doppi o virgolette esterne."""
    text = " ".join((text or "").split())
    quotes = "\"'«»“”‘’"
    if len(text) >= 2 and text[0] in quotes and text[-1] in quotes:
        text = text[1:-1].strip()
    return text


def _pieces(word):
    # "5060ti" -> ['5060', 'ti'], "rtx5060" -> ['rtx', '5060']
    return tuple(re.findall(r"[a-z]+|\d+", word))


def product_terms(text):
    """Parole che un titolo deve contenere per essere «quel prodotto».

    Ogni parola dell'utente diventa una tupla di pezzi, spezzata dove
    lettere e cifre si toccano: così "rtx 5060" riconosce anche "RTX5060"
    e "RTX 5060Ti", e "ps5" riconosce "PS 5". Articoli e preposizioni non
    contano (a meno che non ci sia altro). Lista vuota = testo inutilizzabile.
    """
    words = _normalize(text).split()
    meaningful = [w for w in words if w not in _STOP_TOKENS] or words
    terms = []
    for word in meaningful:
        term = _pieces(word)
        if term and term not in terms:
            terms.append(term)
    if sum(len(p) for term in terms for p in term) < 2:
        return []  # una lettera o una cifra sola: troppo generico
    return terms


def _contains_run(sequence, run):
    n = len(run)
    return any(tuple(sequence[i:i + n]) == run for i in range(len(sequence) - n + 1))


def matches_product(terms, *texts):
    """True se ogni parola del prodotto compare, intera, in uno dei testi.

    `texts` sono titolo e brand dell'annuncio (il brand serve per i titoli
    tipo "Mouse MX Vertical" con brand Logitech). L'ordine delle parole è
    libero, ma una parola è sempre intera: "5060" non corrisponde a "15060"
    e "ps5" non corrisponde a "PS4 con 5 giochi".

    Volutamente nessuna tolleranza su plurali o desinenze: accettare
    "verticale" per "vertical" farebbe passare annunci veri come "Supporto
    verticale per tastiera Logitech MX Keys" cercando "logitech mx vertical".
    """
    if not terms:
        return False
    sequences = [
        [p for word in _normalize(t).split() for p in _pieces(word)]
        for t in texts if t
    ]
    return all(any(_contains_run(seq, term) for seq in sequences) for term in terms)


async def search_brands(text, limit=6):
    """Cerca un brand per nome -> [{'id','title','count'}]."""
    data = await _curl(BRANDS_ENDPOINT, {"keyword": text})
    out = []
    for b in (data.get("brands") or [])[:limit]:
        if b.get("id") is None or not b.get("title"):
            continue
        out.append({
            "id": int(b["id"]),
            "title": str(b["title"]),
            "count": b.get("pretty_item_count") or "",
        })
    return out


async def get_sizes(catalog_id, max_sizes=90):
    """Taglie disponibili per una categoria -> [{'id','title'}] (senza doppioni)."""
    data = await _curl(SIZE_GROUPS_ENDPOINT, {"catalog_ids": str(catalog_id)})
    sizes, seen = [], set()
    for group in data.get("size_groups") or []:
        for s in sorted(group.get("sizes") or [], key=lambda x: x.get("order") or 0):
            sid, title = s.get("id"), (s.get("title") or "").strip()
            if sid is None or not title or sid in seen:
                continue
            seen.add(sid)
            sizes.append({"id": int(sid), "title": title})
    return sizes[:max_sizes]


# --------------------------------------------------------------------------
# Link di ricerca
# --------------------------------------------------------------------------

def _encode_pairs(base, pairs):
    # '[' e ']' restano leggibili: è il formato che usa Vinted stesso
    # (catalog[]=260&brand_ids[]=14) e Telegram lo rende cliccabile.
    query = "&".join(f"{quote(k, safe='[]')}={quote(str(v), safe='')}" for k, v in pairs)
    return f"{base}/catalog?{query}"


def build_search_url(catalog_id=None, brand_id=None, size_ids=None, search_text=None,
                     base_url=BASE_URL):
    """Link Vinted con i filtri scelti, ordinato dai più recenti."""
    pairs = []
    if search_text:
        pairs.append(("search_text", search_text))
    if catalog_id:
        pairs.append(("catalog[]", catalog_id))
    if brand_id:
        pairs.append(("brand_ids[]", brand_id))
    for sid in size_ids or []:
        pairs.append(("size_ids[]", sid))
    pairs.append(("order", "newest_first"))
    return _encode_pairs(base_url, pairs)


def looks_like_url(text):
    text = (text or "").strip().lower()
    return text.startswith(("http://", "https://", "www.vinted.", "vinted."))


def normalize_search_url(url):
    """Valida un link di ricerca Vinted incollato dall'utente e lo ripulisce.

    Ritorna (url_normalizzato, None) oppure (None, messaggio_errore).
    Tiene tutti i filtri così come sono (anche colore, condizioni, prezzo...),
    toglie pagina/ordinamento e forza l'ordine "più recenti".
    """
    raw = (url or "").strip()
    if not raw:
        return None, "Il link è vuoto."
    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw
    try:
        parts = urlsplit(raw)
    except ValueError:
        return None, "Non riesco a leggere questo link."
    host = (parts.hostname or "").lower()
    if not _VINTED_HOST_RE.match(host):
        return None, "Non è un link di Vinted."
    if not host.startswith("www."):
        host = "www." + host
    path = parts.path.rstrip("/")
    if path != "/catalog" and not path.startswith("/catalog/"):
        return None, (
            "Mi serve il link di una ricerca Vinted (quello che inizia con "
            "vinted.it/catalog?...), non quello di un annuncio o di un profilo."
        )

    pairs = [
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k not in _NON_FILTER_PARAMS
    ]
    # Categoria nel percorso: /catalog/1846-jeans -> catalog[]=1846
    m = re.match(r"^/catalog/(\d+)", path)
    if m and not any(k == "catalog[]" for k, _ in pairs):
        pairs.insert(0, ("catalog[]", m.group(1)))
    if not pairs:
        return None, (
            "Questo link non ha nessun filtro: imposta almeno una categoria, "
            "un brand o una parola di ricerca su Vinted e poi incollalo di nuovo."
        )
    pairs.append(("order", "newest_first"))
    return _encode_pairs(f"https://{host}", pairs), None


def describe_search_url(url, flat_catalogs=None):
    """Descrizione breve di un link incollato, es. 'Jeans skinny · 1 brand · max 30€'."""
    pairs = parse_qsl(urlsplit(url).query)
    by_id = {c["id"]: c for c in (flat_catalogs or [])}
    parts = []
    text = next((v for k, v in pairs if k == "search_text" and v.strip()), None)
    if text:
        parts.append(f"«{text.strip()}»")
    cats = [v for k, v in pairs if k == "catalog[]"]
    names = []
    for c in cats:
        cat = by_id.get(int(c)) if c.isdigit() else None
        if cat:
            names.append(f"{cat['title']} ({cat['path'][0]})" if len(cat["path"]) > 1 else cat["title"])
    if names:
        parts.append(", ".join(names[:3]))
    elif cats:
        parts.append("1 categoria" if len(cats) == 1 else f"{len(cats)} categorie")
    for key, one, many in (
        ("brand_ids[]", "1 brand", "{} brand"),
        ("size_ids[]", "1 taglia", "{} taglie"),
        ("color_ids[]", "1 colore", "{} colori"),
        ("status_ids[]", "1 condizione", "{} condizioni"),
        ("material_ids[]", "1 materiale", "{} materiali"),
    ):
        n = sum(1 for k, _ in pairs if k == key)
        if n:
            parts.append(one if n == 1 else many.format(n))
    price_from = next((v for k, v in pairs if k == "price_from"), None)
    price_to = next((v for k, v in pairs if k == "price_to"), None)
    if price_from and price_to:
        parts.append(f"{price_from}–{price_to}€")
    elif price_to:
        parts.append(f"max {price_to}€")
    elif price_from:
        parts.append(f"da {price_from}€")
    return " · ".join(parts) if parts else "Ricerca Vinted personalizzata"


# --------------------------------------------------------------------------
# Parsing pagina risultati
# --------------------------------------------------------------------------

def parse_price(text):
    """'44.95 €' / '24,99 €' / '1.234,50 €' -> float (None se non è un prezzo)."""
    m = re.search(r"\d[\d.,\s ]*", text or "")
    if not m:
        return None
    raw = re.sub(r"[\s ]", "", m.group(0)).rstrip(".,")
    if "," in raw and "." in raw:
        dec = max(raw.rfind(","), raw.rfind("."))
        raw = re.sub(r"[.,]", "", raw[:dec]) + "." + raw[dec + 1:]
    elif "," in raw or "." in raw:
        sep = "," if "," in raw else "."
        head, _, tail = raw.rpartition(sep)
        # Vinted mostra sempre 2 decimali: 3 cifre dopo il separatore = migliaia
        raw = head.replace(sep, "") + (tail if len(tail) == 3 else "." + tail)
    try:
        return float(raw)
    except ValueError:
        return None


# Testo accessibile del link annuncio, es.:
# "Titolo, Brand: adidas, Condizioni: Ottime, Taglia: XL, 44.95 €, 47.90 €"
_CARD_TITLE_RE = re.compile(
    r"^(?P<title>.*?)"
    r"(?:, Brand: (?P<brand>.*?))?"
    r"(?:, Condizioni: (?P<condition>.*?))?"
    r"(?:, Taglia: (?P<size>.*?))?"
    r", (?P<price>\d[\d.,\s ]*)€"
    r"(?:, (?P<total>\d[\d.,\s ]*)€)?\s*$",
    re.S,
)


def parse_catalog_items(html, base_url=BASE_URL):
    """Annunci della griglia di una pagina /catalog, nell'ordine della pagina.

    Ritorna [{'id','title','brand','size','condition','price','total_price',
    'url'}]; i campi non trovati sono None. Scarta le card senza id.
    """
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    items, seen = [], set()
    for card in soup.select('[data-testid="grid-item"]'):
        link = card.select_one('a[href*="/items/"]')
        if not link:
            continue
        m = re.search(r"/items/(\d+)", link.get("href", ""))
        if not m:
            continue
        item_id = int(m.group(1))
        if item_id in seen:
            continue
        seen.add(item_id)

        href = link["href"].split("?")[0]
        if not href.startswith("http"):
            href = base_url.rstrip("/") + href

        info = {"title": None, "brand": None, "size": None, "condition": None,
                "price": None, "total": None}
        cm = _CARD_TITLE_RE.match((link.get("title") or "").strip())
        if cm:
            info.update({k: (v.strip() if v else None) for k, v in cm.groupdict().items()})

        def text_of(suffix):
            el = card.select_one(f'[data-testid$="{suffix}"]')
            return el.get_text(" ", strip=True) if el else None

        price = parse_price(info["price"]) if info["price"] else parse_price(text_of("--price-text"))
        total = parse_price(info["total"]) if info["total"] else parse_price(text_of("total-combined-price"))
        brand = info["brand"] or text_of("--description-title")
        title = info["title"]
        if not title:
            # Fallback: titolo dallo slug del link (…/items/123-jeans-levis-501)
            slug = re.sub(r"^.*/items/\d+-?", "", href)
            title = slug.replace("-", " ").strip().capitalize() or brand or "Annuncio Vinted"

        items.append({
            "id": item_id,
            "title": title,
            "brand": brand,
            "size": info["size"],
            "condition": info["condition"],
            "price": price,
            "total_price": total,
            "url": href,
        })
    return items


# --------------------------------------------------------------------------
# Regola "annuncio nuovo"
# --------------------------------------------------------------------------

def detect_new_items(items, seen_ids, watermark, margin):
    """Decide quali annunci della pagina sono davvero nuovi.

    Gli id Vinted crescono nel tempo. Un annuncio è nuovo se non è già
    stato visto E il suo id è sopra `watermark - margin`, dove `watermark`
    è l'id più alto visto finora per questa ricerca. Il margine tollera gli
    annunci che compaiono con qualche minuto di ritardo; la soglia esclude
    invece i vecchi annunci che rientrano in prima pagina (rimessi in
    evidenza, o risaliti dalla seconda pagina quando altri vengono venduti).

    Ritorna (nuovi_in_ordine_cronologico, nuovo_watermark).
    """
    floor = (watermark or 0) - margin
    new = [it for it in items if it["id"] not in seen_ids and it["id"] > floor]
    new.sort(key=lambda it: it["id"])
    top = max([it["id"] for it in items], default=0)
    return new, max(watermark or 0, top)
