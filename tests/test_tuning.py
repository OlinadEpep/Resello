"""Test Idea 2A: CONFIG override + tune_thresholds replay."""

import json
import sqlite3
from deal_hunter.jev import judge as deal_judge


def test_config_override_via_env(monkeypatch):
    import importlib
    monkeypatch.setenv("JEV_THRESHOLDS_JSON", json.dumps({"SKIP_NOUL": 0.99, "W_BRK": 0.1}))
    importlib.reload(deal_judge)
    try:
        assert deal_judge.CONFIG["SKIP_NOUL"] == 0.99
        assert deal_judge.CONFIG["W_BRK"] == 0.1
        assert deal_judge.CONFIG["SKIP_RISK"] == 0.6  # non toccato
    finally:
        monkeypatch.delenv("JEV_THRESHOLDS_JSON", raising=False)
        importlib.reload(deal_judge)


def test_tune_script_runs_on_empty_db(tmp_path):
    import subprocess, sys
    db = tmp_path / "empty.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE jev_signals (query TEXT, title TEXT, pm_choice TEXT, pm_conf REAL,"
        " acc REAL, brk REAL, box REAL, dq REAL, dq_conf REAL, risk REAL,"
        " decision TEXT, notified INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    con.commit()
    con.close()
    r = subprocess.run([sys.executable, "scripts/tune_thresholds.py", "--db", str(db)],
                       capture_output=True, text=True, cwd=".")
    assert r.returncode == 0
    assert "Dati insufficienti" in r.stdout


def test_tune_script_proposes_thresholds(tmp_path):
    import subprocess, sys
    db = tmp_path / "full.db"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE jev_signals (query TEXT, title TEXT, pm_choice TEXT, pm_conf REAL,"
        " acc REAL, brk REAL, box REAL, dq REAL, dq_conf REAL, risk REAL,"
        " decision TEXT, notified INTEGER, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
    )
    for i in range(60):
        con.execute(
            "INSERT INTO jev_signals (query, title, pm_choice, pm_conf, acc, brk, box, dq,"
            " dq_conf, risk, decision, notified) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("q", f"t{i}", "exact_same_product", 0.9, 0.05, 0.02, 0.01, 2.0, 0.8, 0.05,
             "notify", 1),
        )
    con.commit()
    con.close()
    r = subprocess.run([sys.executable, "scripts/tune_thresholds.py", "--db", str(db),
                        "--min-n", "50"], capture_output=True, text=True, cwd=".")
    assert r.returncode == 0
    assert "JEV_THRESHOLDS_JSON" in r.stdout
