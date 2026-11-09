"""Tuning offline delle soglie Jev sui segnali reali (Idea 2A).

Legge jev_signals da deals.db, stampa distribuzioni e propone un
JEV_THRESHOLDS_JSON da mettere in .env. Non chiama nessuna API:
replay deterministico delle decisioni storiche con soglie diverse.

Uso:
    ./venv/bin/python3 scripts/tune_thresholds.py [--db deals.db] [--min-n 50]
"""

import argparse
import json
import sqlite3
import sys


def load_signals(db_path):
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT query, title, pm_choice, pm_conf, acc, brk, box, dq, dq_conf,"
            " risk, decision, notified, created_at FROM jev_signals"
            " ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.OperationalError as e:
        print(f"Tabella jev_signals assente o illeggibile: {e}")
        return []
    finally:
        con.close()


def decide(row, cfg):
    pm, pm_conf = row.get("pm_choice", ""), row.get("pm_conf") or 0
    acc, brk, box = row.get("acc") or 0, row.get("brk") or 0, row.get("box") or 0
    dq_conf = row.get("dq_conf") or 0
    risk = (cfg["W_ACC_BOX"] * max(acc, box) + cfg["W_BRK"] * brk
            + cfg["W_PM"] * (1.0 if pm in ("accessory_only", "unrelated") else 0.0))
    if pm in ("accessory_only", "unrelated") and pm_conf >= cfg["PM_SKIP_CONF"]:
        return "skip"
    if max(acc, box, brk) >= cfg["SKIP_NOUL"]:
        return "skip"
    if risk >= cfg["SKIP_RISK"]:
        return "skip"
    if pm_conf < cfg["PM_MIN_CONF"] or (cfg["REVIEW_LO"] < risk < cfg["REVIEW_HI"]) or dq_conf < cfg["DQ_MIN_CONF"]:
        return "review"
    return "notify"


def summarize(rows, cfg, label):
    from collections import Counter
    dec = Counter(decide(r, cfg) for r in rows)
    n = max(1, len(rows))
    print(f"\n[{label}] n={len(rows)} "
          f"notify={dec['notify']/n:.0%} review={dec['review']/n:.0%} skip={dec['skip']/n:.0%}")
    return dec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="deals.db")
    ap.add_argument("--min-n", type=int, default=50)
    args = ap.parse_args()

    rows = load_signals(args.db)
    print(f"Segnali Jev in {args.db}: {len(rows)}")
    if len(rows) < args.min_n:
        print(f"Dati insufficienti per tuning (min {args.min_n}). "
              f"Il bot sta comunque loggando: riprova tra qualche giorno.")
        print("Distribuzione attuale salvata dai verdict:")
        from collections import Counter
        print(Counter(r.get("decision", "?") for r in rows))
        return 0

    base = {"W_ACC_BOX": 0.45, "W_BRK": 0.35, "W_PM": 0.20, "SKIP_NOUL": 0.7,
            "SKIP_RISK": 0.6, "REVIEW_LO": 0.4, "REVIEW_HI": 0.6,
            "PM_MIN_CONF": 0.5, "PM_SKIP_CONF": 0.6, "DQ_MIN_CONF": 0.5}
    summarize(rows, base, "soglie attuali")

    # Grid search leggera su SKIP_NOUL x SKIP_RISK: obiettivo review 10-20%
    # con skip stabile (i segnali non hanno label umane: si ottimizza la forma,
    # la verità resta una revisione manuale del report per query).
    best, best_cfg = None, None
    for sn in (0.6, 0.65, 0.7, 0.75, 0.8):
        for sr in (0.5, 0.55, 0.6, 0.65, 0.7):
            cfg = dict(base, SKIP_NOUL=sn, SKIP_RISK=sr)
            from collections import Counter
            dec = Counter(decide(r, cfg) for r in rows)
            n = len(rows)
            rev = dec["review"] / n
            score = abs(rev - 0.15)  # target review ~15%
            if best is None or score < best:
                best, best_cfg = score, (sn, sr, dict(dec))
    sn, sr, dec = best_cfg
    print(f"\nProposta: SKIP_NOUL={sn} SKIP_RISK={sr} -> {dec}")
    out = dict(base, SKIP_NOUL=sn, SKIP_RISK=sr)
    print("\nMetti in .env:")
    print(f'JEV_THRESHOLDS_JSON={json.dumps(out)}')
    print("\nPoi riavvia il bot. Nessuna re-inferenza necessaria (pesi in codice).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
