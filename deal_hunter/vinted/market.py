from vinted_scraper import AsyncVintedScraper
from deal_hunter.vinted.playwright_scraper import VintedPlaywrightScraper
from deal_hunter.core.utils import (
    is_valid_item,
    compute_service_fee,
    DEFAULT_SHIPPING,
    extract_dynamic_keywords,
    robust_median,
    sanity_check_market,
)
import statistics
import logging
import asyncio
import time

logger = logging.getLogger(__name__)


def _norm_market_res(res):
    """Normalizza 2-tuple legacy -> 3-tuple (price, kws, meta)."""
    if not res:
        return None, [], {'reason': 'empty_res'}
    if len(res) == 3:
        return res[0], res[1] or [], res[2] or {}
    return res[0], (res[1] or []), {'reason': 'legacy_2tuple'}


def _to_total_items(items):
    """Normalizza VintedItem -> [{'title','price' totale,'url'}]."""
    out = []
    for item in items:
        try:
            base_p = float(item.price)
            service_fee = getattr(item, 'service_fee', None)
            if service_fee is None:
                service_fee = compute_service_fee(base_p)
            else:
                service_fee = float(service_fee)
            total_p = base_p + service_fee + DEFAULT_SHIPPING
            out.append({'title': item.title, 'price': total_p,
                        'url': getattr(item, 'url', '')})
        except (ValueError, TypeError):
            continue
    # Dedup solo su url reale; senza url (mock/test) tieni tutto per non
    # collassare listing distinti con stesso titolo+prezzo.
    seen, dedup = set(), []
    for d in out:
        url = d.get('url')
        if not url:
            dedup.append(d)
            continue
        if url in seen:
            continue
        seen.add(url)
        dedup.append(d)
    return dedup


async def _fetch_multipage(scraper, query, pages=2, per_page=48):
    """Fase 1: 2 pagine invece di 1 per ridurre varianza rilevanza."""
    all_items, seen_ids = [], set()
    for page in range(1, pages + 1):
        try:
            params = {"search_text": query, "page": page, "per_page": per_page}
            items = await scraper.search(params)
        except Exception as e:
            logger.debug(f"page {page} search error for '{query}': {e}")
            break
        if not items:
            break
        fresh = 0
        for it in items:
            uid = getattr(it, 'url', None) or (getattr(it, 'title', ''), getattr(it, 'price', ''))
            if uid in seen_ids:
                continue
            seen_ids.add(uid)
            all_items.append(it)
            fresh += 1
        if fresh == 0:
            break
    return all_items


async def _apply_jev_market_filter(query, items_data):
    """Fase 2: un solo system_one per candidato, 4 domande parallele.

    Codice padrone: soglie da deal_judge.CONFIG. Fail-open: se Jev non
    configurato/errore -> ritorna input invariato con reason fallback.
    """
    try:
        from deal_hunter.jev.judge import get_judge
        jev = get_judge()
    except Exception as e:
        logger.debug(f"Jev market filter non disponibile: {e}")
        return items_data, {'n_raw': len(items_data), 'n_kept': len(items_data),
                            'reason': 'jev_not_configured'}
    if jev is None:
        return items_data, {'n_raw': len(items_data), 'n_kept': len(items_data),
                            'reason': 'jev_not_configured'}
    try:
        kept, meta = await jev.filter_market_candidates(query, items_data)
        return kept, meta
    except Exception as e:
        logger.warning(f"Jev market filter error: {e} -> fail-open")
        return items_data, {'n_raw': len(items_data), 'n_kept': len(items_data),
                            'reason': f'jev_error_fallback:{e}'}


def _pipeline_median(query, items_data):
    """Pipeline condivisa API/fallback. Ritorna (price|None, kws, meta)."""
    meta = {'n_raw': len(items_data)}
    if not items_data:
        return None, [], {**meta, 'reason': 'empty'}
    median_raw = round(statistics.median([d['price'] for d in items_data]), 2)
    meta['median_raw'] = median_raw

    dynamic_kws = extract_dynamic_keywords(items_data, query)
    meta['dynamic_kws'] = dynamic_kws

    regex_kept = [
        d for d in items_data
        if is_valid_item(d['title'], d['price'], dynamic_keywords=dynamic_kws,
                         for_market_calc=True)
    ]
    meta['n_regex_kept'] = len(regex_kept)
    if not regex_kept:
        return None, dynamic_kws, {**meta, 'reason': 'all_filtered_regex'}
    return regex_kept, dynamic_kws, meta


async def get_vinted_market_price(query):
    """Stima robusta prezzo mercato. Ritorna (price|None, kws, meta).

    Compat: i vecchi caller che spacchettano 2 valori continuano a
    funzionare se prendono i primi due (bot.py aggiornato a 3).
    Fasi: multipagina -> regex/dynamic -> Jev full-product -> IQR -> sanity.
    """
    from deal_hunter.core.utils import extract_dynamic_keywords as _edk  # noqa (re-export stabile)
    t0 = time.monotonic()
    scraper = await AsyncVintedScraper.create("https://www.vinted.it")
    try:
        async with scraper:
            try:
                items = await _fetch_multipage(scraper, query)
            except Exception as e:
                logger.error(f"Vinted market price error for '{query}': {e}, trying fallback...")
                return _norm_market_res(await _get_vinted_market_price_fallback(query))

            if not items:
                return _norm_market_res(await _get_vinted_market_price_fallback(query))

            items_data = _to_total_items(items)
            if not items_data:
                return _norm_market_res(await _get_vinted_market_price_fallback(query))

            step, dynamic_kws, meta = _pipeline_median(query, items_data)
            if step is None:
                return _norm_market_res(await _get_vinted_market_price_fallback(query))
            regex_kept = step

            # Fase 2 — filtro semantico Jev prima della mediana
            jev_kept, jev_meta = await _apply_jev_market_filter(query, regex_kept)
            meta = {**meta, **{f'jev_{k}': v for k, v in jev_meta.items()}}
            full_product = jev_kept if jev_kept else regex_kept
            meta['n_full'] = len(full_product)
            if jev_meta.get('reason') == 'jev_filtered' and not jev_kept:
                logger.warning(f"Jev ha scartato tutto per '{query}', fallback su regex_kept")
                meta['jev_empty_fallback'] = True

            # Fase 0+1 — mediana robusta IQR con guardrail
            price, robust_meta = robust_median([d['price'] for d in full_product])
            meta = {**meta, **{f'robust_{k}': v for k, v in robust_meta.items()}}
            if price is None:
                logger.warning(f"Market '{query}' scartato: {robust_meta.get('reason')} "
                               f"(raw={meta.get('median_raw')}, n_full={len(full_product)})")
                fb = _norm_market_res(await _get_vinted_market_price_fallback(query))
                # Se anche fallback fallisce, ritorna None ma con meta diagnostica
                if not fb or not fb[0]:
                    return None, dynamic_kws, meta
                return fb[0], fb[1], {**meta, 'fallback_used': True,
                                      'fallback_price': fb[0]}

            # Fase 3 — banda di sanità per categoria
            ok, reason, suggestion = sanity_check_market(query, price)
            meta['sanity'] = reason
            if not ok:
                logger.warning(f"Market '{query}' fuori banda: {price}€ ({reason}), "
                               f"suggerimento: '{suggestion}'")
                return None, dynamic_kws, {**meta, 'suggested_query': suggestion}

            logger.info(f"Market price for '{query}' via API in {time.monotonic()-t0:.1f}s: "
                        f"{price}€ (raw={meta.get('median_raw')}, n_raw={meta.get('n_raw')}, "
                        f"n_full={len(full_product)})")
            try:
                _log_market_estimate(query, price, meta)
            except Exception as e:
                logger.debug(f"market_estimates write skipped: {e}")
            return round(price, 2), dynamic_kws, meta
    except Exception as e:
        logger.error(f"Vinted market price error for '{query}': {e}, trying fallback...")
        return _norm_market_res(await _get_vinted_market_price_fallback(query))


def _log_market_estimate(query, price, meta):
    """Fase 4: osservabilità best-effort su sqlite (mai bloccare)."""
    import sqlite3
    import json
    import os
    db_path = os.getenv("DEALS_DB_PATH", "deals.db")
    if not os.path.exists(db_path):
        return
    con = sqlite3.connect(db_path, timeout=5)
    try:
        con.execute(
            "INSERT INTO market_estimates (query, market_price, n_raw, n_full,"
            " median_raw, reason, meta) VALUES (?,?,?,?,?,?,?)",
            (query, price, meta.get('n_raw'), meta.get('n_full'),
             meta.get('median_raw'), str(meta.get('robust_reason', meta.get('sanity', ''))),
             json.dumps(meta, default=str)[:4000]),
        )
        con.commit()
    finally:
        con.close()

async def _get_vinted_market_price_fallback(query):
    logger.info(f"Using Playwright fallback for Vinted market price: {query}")
    vp_scraper = VintedPlaywrightScraper()
    await vp_scraper.start()
    try:
        res = await vp_scraper.get_market_price(query)
        # Normalizza 2- o 3-tupla del fallback
        if not res:
            return None, [], {'reason': 'fallback_empty'}
        if len(res) == 3:
            price, kws, meta = res
            # Applica comunque Jev + sanity sul fallback se prezzo grezzo
            if price is not None:
                ok, reason, suggestion = sanity_check_market(query, price)
                meta = {**meta, 'sanity': reason}
                if not ok:
                    return None, kws, {**meta, 'suggested_query': suggestion}
            return price, kws, meta
        price, kws = res
        return price, kws, {'reason': 'fallback_legacy_2tuple'}
    except Exception as e:
        logger.error(f"Vinted market price fallback error for '{query}': {e}")
        return None, [], {'reason': f'fallback_error:{e}'}
    finally:
        await vp_scraper.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    q = "Nintendo 3DS"
    price, kws, meta = asyncio.run(get_vinted_market_price(q))
    print(f"Vinted Market Price for '{q}': {price}€")
    print(f"Detected Negative Keywords: {kws}")
    print(f"Meta: {meta}")
