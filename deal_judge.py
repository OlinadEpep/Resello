"""Filtro semantico deal con TypeSafe Jev (System One).

Il codice resta padrone di scraping/DB/notifiche; Jev fornisce solo
giudizi tipati paralleli sullo stesso state, composti qui in codice
con pesi e confidence-gating.

Domande (una sola chiamata system_one per annuncio):
- product_match: Choice exact_same_product / same_model_variant / accessory_only / unrelated
- is_accessory_only, is_broken_or_parts, is_empty_box_or_manual: Noul
- deal_quality: Score su 3 livelli

Se TYPESAFE_API_KEY manca o l'SDK non risponde -> fallback aperto
(decision=notify, reason=fallback) così il vecchio filtro regex resta attivo.
"""

import hashlib
import json
import logging
import os
import time

logger = logging.getLogger(__name__)

DB_PATH = os.getenv("DEALS_DB_PATH", "deals.db")
CACHE_TTL_SECONDS = 7 * 24 * 3600  # riusa giudizio stessa URL per 7gg

# Idea 1 — rerank pre-fetch: config da env, scelta utente top_k=8.
try:
    RERANK_TOP_K = int(os.getenv("RERANK_TOP_K", "8"))
except ValueError:
    RERANK_TOP_K = 8
try:
    RERANK_MIN_SCORE = float(os.getenv("RERANK_MIN_SCORE", "0.25"))
except ValueError:
    RERANK_MIN_SCORE = 0.25
RERANK_ENABLED = os.getenv("RERANK_ENABLED", "1") == "1"

# Idea 2A — pesi/soglie centralizzati (default = valori storici hardcoded).
# Override via JEV_THRESHOLDS_JSON='{"SKIP_NOUL": 0.75, ...}' senza toccare codice.
CONFIG = {
    "W_ACC_BOX": 0.45,
    "W_BRK": 0.35,
    "W_PM": 0.20,
    "SKIP_NOUL": 0.7,
    "SKIP_RISK": 0.6,
    "REVIEW_LO": 0.4,
    "REVIEW_HI": 0.6,
    "PM_MIN_CONF": 0.5,
    "PM_SKIP_CONF": 0.6,
    "DQ_MIN_CONF": 0.5,
    # Fase 2 — filtro mercato: solo full product entra nella mediana.
    # Override via JEV_THRESHOLDS_JSON senza toccare codice.
    "MARKET_PM_CONF": 0.6,
    "MARKET_MAX_NOUL": 0.7,
}
try:
    _ov = json.loads(os.getenv("JEV_THRESHOLDS_JSON", "") or "{}")
    if isinstance(_ov, dict):
        for _k, _v in _ov.items():
            if _k in CONFIG:
                CONFIG[_k] = float(_v)
except Exception as _e:
    logger.debug(f"JEV_THRESHOLDS_JSON ignorato: {_e}")

_judge_singleton = None


def get_judge():
    """Ritorna il giudice condiviso, o None se Jev non è configurato."""
    global _judge_singleton
    if _judge_singleton is not None:
        return _judge_singleton
    api_key = os.getenv("TYPESAFE_API_KEY")
    if not api_key:
        return None
    try:
        from typesafe_sdk import AsyncTypeSafeClient  # noqa: F401
    except Exception as e:
        logger.warning(f"typesafe-sdk non installato: {e}")
        return None
    _judge_singleton = DealJudge(api_key=api_key, model=os.getenv("TYPESAFE_MODEL", "jev-latest"))
    return _judge_singleton


def reset_judge_for_tests():
    global _judge_singleton
    _judge_singleton = None


def _url_hash(url):
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:32]


def _build_questions():
    from typesafe_sdk import Choice, Noul, Score

    questions = {
        "product_match": Choice(
            instructions=(
                "Given `listing.title` and the wanted `query`, which option best "
                "describes what the listing sells? Focus on the main object, "
                "not price. Listings in Italian, English, Spanish or French."
            ),
            criteria={
                "exact_same_product": (
                    "The complete product the user wants (same model, full item). "
                    "Es: query 'Nintendo 3DS' title 'Nintendo 3DS XL blu funzionante'."
                ),
                "same_model_variant": (
                    "Same product family, different variant/storage/color/bundle, "
                    "still the full product. Es: 'Nintendo 3DS + 2 giochi'."
                ),
                "accessory_only": (
                    "Only an accessory, case, charger, manual, empty box, cover, "
                    "poster or spare part, not the full product. Es: 'custodia per 3DS', "
                    "'cover 3DS', 'scatola vuota 3DS', 'manuale/manual 3DS', "
                    "'caja vacía', 'boîte vide', 'charger only'."
                ),
                "unrelated": (
                    "Different product or unclear. Es: 'Nintendo Switch' per query 3DS."
                ),
            },
        ),
        "is_accessory_only": Noul(
            instructions=(
                "Is `listing.title` selling ONLY an accessory, case, cover, "
                "charger, manual, poster or empty box instead of the full product in `query`?"
            ),
        ),
        "is_broken_or_parts": Noul(
            instructions=(
                "Is `listing.title` broken, for parts/spare, not working? "
                "Segnali: 'rotto', 'broken', 'not working', 'non funzionante', "
                "'pezzi', 'ricambi', 'parts', 'for parts', 'roto', 'cassé', 'only'."
            ),
        ),
        "is_empty_box_or_manual": Noul(
            instructions=(
                "Is `listing.title` ONLY an empty box, case or manual/instructions? "
                "Segnali: 'scatola vuota', 'empty box', 'box only', 'solo scatola', "
                "'manuale', 'manual', 'istruzioni', 'boîte vide', 'caja vacía'."
            ),
        ),
        "deal_quality": Score(
            instructions=(
                "Given `listing.total_price`, `market.market_price` and "
                "`market.threshold`, how good is the deal for the full product?"
            ),
            criteria=[
                "Not a deal: price at or above market, or listing is not the full product.",
                "Fair deal: clearly below market and under threshold.",
                "Exceptional deal: far below market, rare opportunity.",
            ],
        ),
    }
    return questions


class DealJudge:
    def __init__(self, api_key=None, model="jev-latest", db_path=None):
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY")
        self.model = model or os.getenv("TYPESAFE_MODEL", "jev-latest")
        self.db_path = db_path or DB_PATH
        self._mem_cache = {}  # url_hash -> (verdict, ts)

    def _cache_get(self, url_hash):
        hit = self._mem_cache.get(url_hash)
        if hit and time.time() - hit[1] < CACHE_TTL_SECONDS:
            return hit[0]
        return None

    def _cache_set(self, url_hash, verdict):
        self._mem_cache[url_hash] = (verdict, time.time())
        try:
            import sqlite3

            if os.path.exists(self.db_path):
                con = sqlite3.connect(self.db_path, timeout=5)
                try:
                    con.execute(
                        "INSERT OR REPLACE INTO jev_cache (url_hash, url, verdict) VALUES (?, ?, ?)",
                        (url_hash, verdict.get("url", ""), json.dumps(verdict)),
                    )
                    con.commit()
                finally:
                    con.close()
        except Exception as e:
            logger.debug(f"jev_cache write skipped: {e}")

    async def judge(self, query, title, total_price, market_price=None, threshold=None, url=""):
        """Giudica un annuncio. Ritorna dict con decision notify|review|skip."""
        url_hash = _url_hash(url or f"{query}|{title}|{total_price}")
        cached = self._cache_get(url_hash)
        if cached is not None:
            return cached

        from typesafe_sdk import AsyncTypeSafeClient

        state = {
            "query": query,
            "listing": {"title": title, "total_price": total_price},
            "market": {"market_price": market_price, "threshold": threshold},
        }
        questions = _build_questions()
        try:
            async with AsyncTypeSafeClient(api_key=self.api_key) as client:
                response = await client.system_one(
                    state=state, questions=questions, model=self.model, timeout=15.0
                )
        except Exception as e:
            logger.warning(f"Jev system_one error: {e} -> fallback notify")
            verdict = {"decision": "notify", "reason": f"jev_error_fallback: {e}",
                       "url": url, "query": query, "title": title}
            return verdict

        answers = response.answers or {}
        try:
            pm = answers["product_match"]
            pm_choice = getattr(pm, "choice", "same_model_variant")
            pm_conf = float(getattr(pm, "confidence", 0.0) or 0.0)
            acc = float(getattr(answers["is_accessory_only"], "noul", 0.0) or 0.0)
            brk = float(getattr(answers["is_broken_or_parts"], "noul", 0.0) or 0.0)
            box = float(getattr(answers["is_empty_box_or_manual"], "noul", 0.0) or 0.0)
            dq = answers["deal_quality"]
            dq_score = float(getattr(dq, "score", 1.0) or 0.0)
            dq_conf = float(getattr(dq, "confidence", 0.0) or 0.0)
        except Exception as e:
            logger.warning(f"Jev answer parse error: {e} -> fallback notify")
            return {"decision": "notify", "reason": "parse_fallback", "url": url}

        # Composite in codice: segnali indipendenti pesati (Idea 2A: da CONFIG)
        risk = (CONFIG["W_ACC_BOX"] * max(acc, box) + CONFIG["W_BRK"] * brk
                + CONFIG["W_PM"] * (1.0 if pm_choice in ("accessory_only", "unrelated") else 0.0))

        if pm_choice in ("accessory_only", "unrelated") and pm_conf >= CONFIG["PM_SKIP_CONF"]:
            verdict = {"decision": "skip", "reason": f"product_match={pm_choice} conf={pm_conf:.2f}"}
        elif max(acc, box, brk) >= CONFIG["SKIP_NOUL"]:
            verdict = {"decision": "skip", "reason": f"noul acc={acc:.2f} box={box:.2f} brk={brk:.2f}"}
        elif risk >= CONFIG["SKIP_RISK"]:
            verdict = {"decision": "skip", "reason": f"composite_risk={risk:.2f}"}
        elif pm_conf < CONFIG["PM_MIN_CONF"] or (CONFIG["REVIEW_LO"] < risk < CONFIG["REVIEW_HI"]) or dq_conf < CONFIG["DQ_MIN_CONF"]:
            verdict = {"decision": "review", "reason": f"uncertain risk={risk:.2f} pm_conf={pm_conf:.2f} dq_conf={dq_conf:.2f}"}
        else:
            verdict = {"decision": "notify", "reason": f"ok risk={risk:.2f} deal_score={dq_score:.2f}"}

        verdict.update({
            "url": url, "query": query, "title": title,
            "product_match": pm_choice, "product_match_confidence": pm_conf,
            "is_accessory_only": acc, "is_broken_or_parts": brk,
            "is_empty_box_or_manual": box, "deal_quality": dq_score,
            "deal_quality_confidence": dq_conf, "risk": risk,
            "total_price": total_price, "market_price": market_price,
            "threshold": threshold,
        })
        self._cache_set(url_hash, verdict)
        self.log_signal(verdict, notified=(verdict["decision"] in ("notify", "review")))
        try:
            usage = getattr(response, "usage", None)
            if usage:
                logger.info(f"Jev usage in={getattr(usage,'input_tokens',0)} out={getattr(usage,'output_tokens',0)}")
        except Exception:
            pass
        return verdict

    # ---- Idea 2A: logging segnali per tuning futuro (best-effort) ----
    def log_signal(self, verdict, notified=False):
        """Scrive una riga in jev_signals. Mai bloccare il flusso: fail-open."""
        try:
            import sqlite3

            if not os.path.exists(self.db_path):
                return
            con = sqlite3.connect(self.db_path, timeout=5)
            try:
                con.execute(
                    "INSERT INTO jev_signals (query, title, url_hash, base_price, total_price,"
                    " market_price, threshold, pm_choice, pm_conf, acc, brk, box, dq, dq_conf,"
                    " risk, decision, reason, notified)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        verdict.get("query", ""), verdict.get("title", ""),
                        _url_hash(verdict.get("url", "")),
                        verdict.get("base_price"), verdict.get("total_price"),
                        verdict.get("market_price"), verdict.get("threshold"),
                        verdict.get("product_match", ""), verdict.get("product_match_confidence", 0),
                        verdict.get("is_accessory_only", 0), verdict.get("is_broken_or_parts", 0),
                        verdict.get("is_empty_box_or_manual", 0), verdict.get("deal_quality", 0),
                        verdict.get("deal_quality_confidence", 0), verdict.get("risk", 0),
                        verdict.get("decision", ""), verdict.get("reason", ""),
                        1 if notified else 0,
                    ),
                )
                con.commit()
            finally:
                con.close()
        except Exception as e:
            logger.debug(f"jev_signals write skipped: {e}")

    # ---- Idea 1: rerank leggero pre-fetch ----
    async def rank_candidates(self, query, candidates, top_k=None, min_score=None):
        """Riordina candidati (titolo+base_price) con un Noul leggero per coppia.

        Ritorna [(candidate, noul), ...] ordinati per noul desc, filtrati per
        min_score e troncati a top_k. Fallback: ordine originale con score 1.0
        (nessun filtraggio) se Jev non disponibile o errore.
        """
        from typesafe_sdk import AsyncTypeSafeClient, Noul

        top_k = RERANK_TOP_K if top_k is None else top_k
        min_score = RERANK_MIN_SCORE if min_score is None else min_score
        if not candidates:
            return []

        question = Noul(
            instructions=(
                "Given the wanted `query` and one `candidate.title` with its "
                "`candidate.base_price`, could this listing plausibly be the full "
                "wanted product at a good price (not an accessory, empty box, manual, "
                "broken item or unrelated product)? Titles may be Italian, English, "
                "Spanish or French."
            ),
        )

        import asyncio as _asyncio

        sem = _asyncio.Semaphore(5)

        async def _score_one(client, cand):
            async with sem:
                try:
                    resp = await client.system_one(
                        state={"query": query,
                               "candidate": {"title": cand.get("title", ""),
                                             "base_price": cand.get("price")}},
                        questions={"rel": question},
                        model=self.model, timeout=10.0,
                    )
                    s = float(getattr(resp.answers["rel"], "noul", 0.0) or 0.0)
                    return (cand, s)
                except Exception as e:
                    logger.debug(f"rerank score error: {e} -> 1.0 fallback")
                    return (cand, 1.0)

        try:
            async with AsyncTypeSafeClient(api_key=self.api_key) as client:
                scored = await _asyncio.gather(
                    *(_score_one(client, c) for c in candidates)
                )
        except Exception as e:
            logger.warning(f"rerank fallito: {e} -> ordine originale")
            return [(c, 1.0) for c in candidates]

        scored.sort(key=lambda t: t[1], reverse=True)
        kept = [(c, s) for (c, s) in scored if s >= min_score][:top_k]
        logger.info(f"rerank '{query}': {len(candidates)} -> {len(kept)} (top_k={top_k}, min={min_score})")
        return kept

    # ---- Fase 2: filtro mercato — solo full product entra nella mediana ----
    def _market_questions(self):
        """Choice+Noul paralleli sullo stesso state {query, listing}.

        Codice padrone: Jev dà giudizi tipati, la soglia/confidence-gating
        sta qui in codice (MARKET_PM_CONF, MARKET_MAX_NOUL). Una sola
        chiamata system_one per candidato, 4 domande in parallelo.
        """
        from typesafe_sdk import Choice, Noul

        return {
            "product_match": Choice(
                instructions=(
                    "Given the wanted `query` and one `listing.title`, which option "
                    "best describes what the listing sells? Focus on the main object. "
                    "Titles may be Italian, English, Spanish or French."
                ),
                criteria={
                    "exact_same_product": (
                        "The complete wanted product (same model, full item). "
                        "Es: query 'playstation 5' title 'PS5 slim lettore disco'."
                    ),
                    "same_model_variant": (
                        "Same product family, different variant/storage/bundle, "
                        "still the full product. Es: 'PS5 + 2 controller'."
                    ),
                    "accessory_only": (
                        "Only accessory, controller, game, cover, case, charger, "
                        "stand, headset, cable, skin, empty box or manual — not the "
                        "full product. Es: 'DualSense controller', 'gioco PS5', "
                        "scatola vuota PS5'."
                    ),
                    "unrelated": "Different product or unclear.",
                },
            ),
            "is_accessory_only": Noul(
                instructions=(
                    "Is `listing.title` selling ONLY an accessory, controller, game, "
                    "cover, charger, stand, headset, cable or skin instead of the "
                    "full product in `query`?"
                ),
            ),
            "is_broken_or_parts": Noul(
                instructions=(
                    "Is `listing.title` broken, for parts/spare, not working?"
                ),
            ),
            "is_empty_box_or_manual": Noul(
                instructions=(
                    "Is `listing.title` ONLY an empty box, case or manual/instructions?"
                ),
            ),
        }

    async def filter_market_candidates(self, query, candidates):
        """Filtra candidati mercato tenendo solo full product.

        candidates: list di dict {'title','price'}. Ritorna (kept, meta).
        kept: sottolista full product. meta: conteggi + reason.
        Fail-open: se Jev non disponibile/errore -> (candidates, reason=fallback).
        """
        if not candidates:
            return [], {'n_raw': 0, 'n_kept': 0, 'reason': 'empty'}
        from typesafe_sdk import AsyncTypeSafeClient
        import asyncio as _asyncio

        questions = self._market_questions()
        pm_min_conf = CONFIG.get("MARKET_PM_CONF", 0.6)
        max_noul = CONFIG.get("MARKET_MAX_NOUL", 0.7)
        sem = _asyncio.Semaphore(5)

        async def _one(client, cand):
            async with sem:
                try:
                    resp = await client.system_one(
                        state={"query": query,
                               "listing": {"title": cand.get("title", ""),
                                           "base_price": cand.get("price")}},
                        questions=questions,
                        model=self.model, timeout=12.0,
                    )
                    a = resp.answers or {}
                    pm = a.get("product_match")
                    pm_choice = getattr(pm, "choice", "unrelated")
                    pm_conf = float(getattr(pm, "confidence", 0.0) or 0.0)
                    acc = float(getattr(a.get("is_accessory_only"), "noul", 0.0) or 0.0)
                    brk = float(getattr(a.get("is_broken_or_parts"), "noul", 0.0) or 0.0)
                    box = float(getattr(a.get("is_empty_box_or_manual"), "noul", 0.0) or 0.0)
                    keep = (
                        pm_choice in ("exact_same_product", "same_model_variant")
                        and pm_conf >= pm_min_conf
                        and max(acc, brk, box) < max_noul
                    )
                    return (cand, keep,
                            {'pm': pm_choice, 'pm_conf': round(pm_conf, 2),
                             'acc': round(acc, 2), 'brk': round(brk, 2),
                             'box': round(box, 2)})
                except Exception as e:
                    logger.debug(f"market filter error: {e} -> keep fallback")
                    return (cand, True, {'pm': 'fallback', 'error': str(e)[:80]})

        try:
            async with AsyncTypeSafeClient(api_key=self.api_key) as client:
                results = await _asyncio.gather(
                    *(_one(client, c) for c in candidates)
                )
        except Exception as e:
            logger.warning(f"market filter fallito: {e} -> tengo tutto (fail-open)")
            return list(candidates), {'n_raw': len(candidates),
                                      'n_kept': len(candidates),
                                      'reason': 'jev_unavailable_fallback'}

        kept = [c for (c, k, _) in results if k]
        details = [d for (_, _, d) in results]
        n_acc = sum(1 for d in details if d.get('pm') == 'accessory_only')
        n_unr = sum(1 for d in details if d.get('pm') == 'unrelated')
        meta = {'n_raw': len(candidates), 'n_kept': len(kept),
                'n_accessory': n_acc, 'n_unrelated': n_unr,
                'reason': 'jev_filtered'}
        logger.info(f"market filter '{query}': {len(candidates)} -> {len(kept)} full product")
        return kept, meta
