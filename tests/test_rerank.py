"""Test Idea 1: rerank pre-fetch."""

import pytest
from types import SimpleNamespace
from deal_hunter.jev import judge as deal_judge


def _rel_client(scores):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def system_one(self, state, questions, model=None, timeout=None):
            title = state["candidate"]["title"]
            return SimpleNamespace(
                answers={"rel": SimpleNamespace(noul=scores.get(title, 0.5))},
                usage=None,
            )

    return FakeClient


async def test_rank_orders_and_truncates(monkeypatch, tmp_path):
    import typesafe_sdk
    scores = {"a buon prodotto": 0.9, "b buono": 0.7, "c junk": 0.1, "d medio": 0.5}
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", _rel_client(scores))
    j = deal_judge.DealJudge(api_key="t", db_path=str(tmp_path / "t.db"))
    cands = [{"title": t, "price": 10.0, "url": t} for t in scores]
    out = await j.rank_candidates("query", cands, top_k=2, min_score=0.2)
    assert [c["title"] for (c, _s) in out] == ["a buon prodotto", "b buono"]


async def test_rank_min_score_filters(monkeypatch, tmp_path):
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", _rel_client({"x": 0.1}))
    j = deal_judge.DealJudge(api_key="t", db_path=str(tmp_path / "t.db"))
    out = await j.rank_candidates("q", [{"title": "x", "price": 1}], top_k=8, min_score=0.25)
    assert out == []


async def test_rank_fallback_on_error(monkeypatch, tmp_path):
    import typesafe_sdk

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("down")

    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", Boom)
    j = deal_judge.DealJudge(api_key="t", db_path=str(tmp_path / "t.db"))
    cands = [{"title": "a", "price": 1}]
    out = await j.rank_candidates("q", cands)
    assert out == [(cands[0], 1.0)]


async def test_rank_empty():
    import tempfile, os
    j = deal_judge.DealJudge(api_key="t", db_path=os.path.join(tempfile.gettempdir(), "no.db"))
    assert await j.rank_candidates("q", []) == []
