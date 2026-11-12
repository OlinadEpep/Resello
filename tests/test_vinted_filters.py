"""Test offline dei filtri Vinted: categorie, link di ricerca, parsing, regola 'nuovo'."""

import pytest

import vinted_filters as vf

TREE = [
    {"id": 5, "title": "Uomo", "catalogs": [
        {"id": 2050, "title": "Vestiti", "catalogs": [
            {"id": 257, "title": "Jeans", "catalogs": [
                {"id": 1817, "title": "Jeans skinny", "catalogs": []},
                {"id": 1818, "title": "Jeans slim fit", "catalogs": []},
            ]},
            {"id": 34, "title": "Pantaloni", "catalogs": [
                {"id": 260, "title": "Pantaloni a gamba larga", "catalogs": []},
            ]},
            {"id": 1242, "title": "Scarpe da ginnastica", "catalogs": []},
            {"id": 79, "title": "Maglioni e pullover", "catalogs": [
                {"id": 267, "title": "Felpe con cappuccio", "catalogs": []},
            ]},
        ]},
    ]},
    {"id": 1904, "title": "Donna", "catalogs": [
        {"id": 4, "title": "Vestiti", "catalogs": [
            {"id": 183, "title": "Jeans", "catalogs": [
                {"id": 1844, "title": "Jeans skinny", "catalogs": []},
            ]},
        ]},
    ]},
]


def _card(item_id, title, brand="adidas", size="XL", cond="Ottime", price="44.95", total="47.90"):
    label = f"{title}, Brand: {brand}, Condizioni: {cond}, Taglia: {size}, {price} €, {total} €"
    return f'''
    <div class="feed-grid__item" data-testid="grid-item">
      <div data-testid="product-item-id-{item_id}">
        <a data-testid="product-item-id-{item_id}--overlay-link"
           href="/items/{item_id}-some-slug?referrer=catalog" title="{label}"></a>
        <p data-testid="product-item-id-{item_id}--description-title">{brand}</p>
        <p data-testid="product-item-id-{item_id}--description-subtitle">{size} · {cond}</p>
        <p data-testid="product-item-id-{item_id}--price-text">{price.replace(".", ",")} €</p>
        <span data-testid="total-combined-price">{total.replace(".", ",")} €</span>
      </div>
    </div>'''


def page(*cards):
    return "<html><body>" + "".join(cards) + "</body></html>"


# --- categorie -------------------------------------------------------------

def test_flatten_catalogs_builds_paths():
    flat = vf.flatten_catalogs(TREE)
    by_id = {c["id"]: c for c in flat}
    assert by_id[1817]["path"] == ["Uomo", "Vestiti", "Jeans", "Jeans skinny"]
    assert vf.category_label(by_id[260]) == "Uomo › Vestiti › Pantaloni › Pantaloni a gamba larga"
    assert len(flat) == 14


def test_search_categories_uses_parent_words_to_disambiguate():
    flat = vf.flatten_catalogs(TREE)
    assert vf.search_categories(flat, "jeans skinny uomo")[0]["id"] == 1817
    assert vf.search_categories(flat, "jeans skinny donna")[0]["id"] == 1844


def test_search_categories_prefers_general_category_for_single_word():
    flat = vf.flatten_catalogs(TREE)
    top_two = {c["id"] for c in vf.search_categories(flat, "jeans")[:2]}
    assert top_two == {257, 183}  # "Jeans" uomo e donna prima delle sottocategorie


def test_search_categories_partial_and_plural_match():
    flat = vf.flatten_catalogs(TREE)
    # l'esempio dell'utente: non esiste "jeans a gamba larga", vince la più vicina
    assert vf.search_categories(flat, "jeans a gamba larga")[0]["id"] == 260
    assert vf.search_categories(flat, "felpa")[0]["id"] == 267  # felpa ~ Felpe
    assert vf.search_categories(flat, "sneakers uomo")[0]["id"] == 1242  # sinonimo
    assert vf.search_categories(flat, "xyzzy") == []
    assert vf.search_categories(flat, "di e per") == []  # solo parole vuote


# --- link di ricerca -------------------------------------------------------

def test_build_search_url_orders_by_newest_and_keeps_all_filters():
    url = vf.build_search_url(catalog_id=260, brand_id=14, size_ids=[207, 208])
    assert url == ("https://www.vinted.it/catalog?catalog[]=260&brand_ids[]=14"
                   "&size_ids[]=207&size_ids[]=208&order=newest_first")
    assert vf.build_search_url(brand_id=14) == \
        "https://www.vinted.it/catalog?brand_ids[]=14&order=newest_first"


def test_build_search_url_with_product_text():
    assert vf.build_search_url(search_text="logitech mx vertical") == \
        "https://www.vinted.it/catalog?search_text=logitech%20mx%20vertical&order=newest_first"
    # il link generato è già nella forma normalizzata (stessa ricerca = stesso link)
    url = vf.build_search_url(search_text="rtx 5060")
    assert vf.normalize_search_url(url) == (url, None)
    assert vf.normalize_search_url("vinted.it/catalog?search_text=rtx+5060&page=3") == (url, None)


def test_normalize_search_url_strips_non_filters_and_forces_newest():
    url, err = vf.normalize_search_url(
        "https://www.vinted.it/catalog?search_text=felpa%20nike&catalog[]=79&color_ids[]=1"
        "&price_to=30&order=price_low_to_high&page=4&time=1794254578&search_id=99"
    )
    assert err is None
    assert url == ("https://www.vinted.it/catalog?search_text=felpa%20nike&catalog[]=79"
                   "&color_ids[]=1&price_to=30&order=newest_first")


def test_normalize_search_url_category_in_path_and_missing_scheme():
    url, err = vf.normalize_search_url("vinted.it/catalog/1846-jeans?brand_ids[]=14")
    assert err is None
    assert url == "https://www.vinted.it/catalog?catalog[]=1846&brand_ids[]=14&order=newest_first"


@pytest.mark.parametrize("bad", [
    "https://www.vinted.it/items/123-jeans",           # annuncio, non ricerca
    "https://www.vinted.it/catalog?order=newest_first",  # nessun filtro
    "https://vinted.it.evil.com/catalog?brand_ids[]=14",  # host finto
    "https://www.google.com/catalog?brand_ids[]=14",
    "",
])
def test_normalize_search_url_rejects(bad):
    url, err = vf.normalize_search_url(bad)
    assert url is None and err


def test_looks_like_url():
    assert vf.looks_like_url("https://www.vinted.it/catalog?x=1")
    assert vf.looks_like_url("www.vinted.it/catalog")
    assert not vf.looks_like_url("jeans skinny uomo")


def test_describe_search_url_names_categories_and_counts_the_rest():
    flat = vf.flatten_catalogs(TREE)
    url = ("https://www.vinted.it/catalog?search_text=baggy&catalog[]=1817&brand_ids[]=14"
           "&size_ids[]=207&size_ids[]=208&price_to=30&order=newest_first")
    assert vf.describe_search_url(url, flat) == \
        "«baggy» · Jeans skinny (Uomo) · 1 brand · 2 taglie · max 30€"
    # senza albero categorie resta comunque una descrizione sensata
    assert vf.describe_search_url(url) == "«baggy» · 1 categoria · 1 brand · 2 taglie · max 30€"


# --- parsing ---------------------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("44.95 €", 44.95), ("24,99 €", 24.99), ("1.234,50 €", 1234.50),
    ("1,234.50 €", 1234.50), ("1.250 €", 1250.0), ("5 €", 5.0),
    ("gratis", None), ("", None), (None, None),
])
def test_parse_price(text, expected):
    assert vf.parse_price(text) == expected


def test_parse_catalog_items_reads_all_fields():
    html = page(_card(10740889937, "Adidas Track Pants, blu, Y2K"), _card(10740870330, "Baggy", size="S", price="13.95", total="15.35"))
    items = vf.parse_catalog_items(html)
    assert [it["id"] for it in items] == [10740889937, 10740870330]
    first = items[0]
    assert first == {
        "id": 10740889937,
        "title": "Adidas Track Pants, blu, Y2K",  # le virgole nel titolo non lo spezzano
        "brand": "adidas", "size": "XL", "condition": "Ottime",
        "price": 44.95, "total_price": 47.90,
        "url": "https://www.vinted.it/items/10740889937-some-slug",
    }
    assert items[1]["price"] == 13.95 and items[1]["size"] == "S"


def test_parse_catalog_items_falls_back_when_label_format_changes():
    html = page('''
    <div data-testid="grid-item">
      <a href="/items/777-jeans-levis-501?referrer=catalog" title="formato sconosciuto"></a>
      <p data-testid="product-item-id-777--description-title">Levi's</p>
      <p data-testid="product-item-id-777--price-text">19,00 €</p>
    </div>''')
    (item,) = vf.parse_catalog_items(html)
    assert item["id"] == 777
    assert item["title"] == "Jeans levis 501"
    assert item["brand"] == "Levi's"
    assert item["price"] == 19.0 and item["total_price"] is None


def test_parse_catalog_items_skips_duplicates_and_cards_without_link():
    html = page(_card(1, "A"), _card(1, "A"), '<div data-testid="grid-item"><p>pubblicità</p></div>')
    assert [it["id"] for it in vf.parse_catalog_items(html)] == [1]
    assert vf.parse_catalog_items("") == []
    assert vf.parse_catalog_items(None) == []


# --- regola "annuncio nuovo" ----------------------------------------------

def _items(*ids):
    return [{"id": i} for i in ids]


def test_detect_new_items_basic():
    new, wm = vf.detect_new_items(_items(1050, 1040, 1000), {1000, 1040}, 1040, margin=100)
    assert [it["id"] for it in new] == [1050]
    assert wm == 1050


def test_detect_new_items_ignores_old_listings_resurfacing():
    # 500 = annuncio vecchio rimesso in evidenza / risalito da pagina 2
    new, wm = vf.detect_new_items(_items(2000, 500), {2000}, 2000, margin=100)
    assert new == [] and wm == 2000


def test_detect_new_items_tolerates_late_appearing_listing_within_margin():
    # 1950 è stato creato prima di 2000 ma compare in ricerca solo ora
    new, _ = vf.detect_new_items(_items(2000, 1950), {2000}, 2000, margin=100)
    assert [it["id"] for it in new] == [1950]


def test_detect_new_items_chronological_and_watermark_never_decreases():
    new, wm = vf.detect_new_items(_items(3003, 3001, 3002), set(), 3000, margin=10)
    assert [it["id"] for it in new] == [3001, 3002, 3003]
    assert wm == 3003
    assert vf.detect_new_items([], set(), 3000, margin=10) == ([], 3000)
    assert vf.detect_new_items(_items(10), {10}, 3000, margin=10) == ([], 3000)


# --- nome esatto di un prodotto ---------------------------------------------

# Titoli (e brand) reali restituiti da Vinted cercando "rtx 5060".
@pytest.mark.parametrize("title,brand,expected", [
    ("Gigabyte RTX 5060 windforce MAX", "Gigabyte", True),
    ("Rtx 5060 low profile", "Gigabyte", True),
    ("RTX 5060TI 8GB OC Gaming Trio New", "MSI", True),          # attaccato al suffisso
    ("PC portable gamer ASUS 16\"RTX 5060-32 Go RAM -1 To", "Asus", True),
    ("Scheda video RTX5060 8 GB", "Asus", True),                 # senza spazio
    ("🔥PC Gaming | i7 12700 | RTX 5060 8Gb | Ram 16GB DDR5", "Custommade", True),
    ("RTX 3070 Ti 8GB Founders Edition", "NVIDIA", False),       # altro modello
    ("Intel ARC B580 Titan Sparkle 12gb ram", "Sparkle", False),
    ("5060ti AERO 8GB scheda video", "Gigabyte", False),         # manca "rtx"
    ("Gaming PC (schaut in die Beschreibung)", "Custommade", False),  # solo in descrizione
    ("RTX 15060 prototipo", None, False),                        # numero diverso
])
def test_matches_product_rtx_5060(title, brand, expected):
    assert vf.matches_product(vf.product_terms("rtx 5060"), title, brand) is expected


@pytest.mark.parametrize("title,brand,expected", [
    ("Logitech mx vertical NUOVO", "Logitech", True),
    ("Souris Mx Vertical Logitech", "Logitech", True),           # ordine libero
    ("Mouse MX vertical", "Logitech", True),                     # "logitech" solo nel brand
    ("Ratón ergonómico inalámbrico Logitech MX Vertical", "Logitech", True),
    ("Mouse Logitech MX Master 2S", "Logitech", False),
    ("Logitech MX Verticale Mouse Wireless Ergonomico", "Logitech", False),  # parola diversa
    ("Supporto verticale per tastiera Logitech MX Key beige", None, False),
    ("Trust Mouse Minimal Design. Art.53926", "Trust", False),
    ("Souris ergonomique", "Logitech", False),
])
def test_matches_product_logitech_mx_vertical(title, brand, expected):
    assert vf.matches_product(vf.product_terms("Logitech MX Vertical"), title, brand) is expected


def test_product_terms_split_letters_and_digits_but_keep_words_whole():
    assert vf.product_terms("RTX 5060") == [("rtx",), ("5060",)]
    assert vf.product_terms("rtx5060") == [("rtx", "5060")]
    ps5 = vf.product_terms("ps5")
    assert vf.matches_product(ps5, "Sony PS 5 slim", None)
    assert vf.matches_product(ps5, "Console PS5 + 2 giochi", None)
    assert not vf.matches_product(ps5, "PS4 con 5 giochi", None)   # i pezzi devono essere vicini
    # accenti, maiuscole e punteggiatura non contano
    assert vf.matches_product(vf.product_terms("caffè"), "Macchina CAFFE' Bialetti", None)
    # il prodotto non può stare a cavallo tra titolo e brand
    assert not vf.matches_product(vf.product_terms("ps5"), "Console PS", "5 stelle")


def test_matches_product_is_strict_on_word_endings():
    def match(product, title):
        return vf.matches_product(vf.product_terms(product), title)

    assert not match("iphone 15 pro", "iPhone 15 Prox")
    assert not match("rtx 5060", "RTX 50601")
    assert not match("galaxy s24", "Galaxy S245")
    assert not match("vertical", "Mouse verticale")       # nome esatto: niente desinenze


def test_product_terms_ignore_fillers_and_reject_useless_text():
    assert vf.product_terms("borsa di gucci") == [("borsa",), ("gucci",)]
    assert vf.product_terms("the") == [("the",)]          # solo parole "vuote": restano
    assert vf.product_terms("rtx rtx 5060") == [("rtx",), ("5060",)]
    for useless in ("", "   ", "?!", "5", "a"):
        assert vf.product_terms(useless) == []
    assert vf.matches_product([], "qualsiasi titolo") is False


def test_clean_product_text():
    assert vf.clean_product_text('  "logitech   mx vertical" ') == "logitech mx vertical"
    assert vf.clean_product_text("«rtx 5060»") == "rtx 5060"
    assert vf.clean_product_text("monitor 27'") == "monitor 27'"   # apostrofo solo in fondo: resta
