"""Test DealJudge con SDK mockato: nessuna chiamata rete."""

import os
import pytest
from types import SimpleNamespace
from deal_hunter.jev import judge as deal_judge


def _fake_client_factory(answers):
    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def system_one(self, state, questions, model=None, timeout=None):
            assert "product_match" in questions  # una sola chiamata, domande parallele
            return SimpleNamespace(answers=answers, usage=None)

    return FakeClient


def _answers(pm="exact_same_product", pm_conf=0.9, acc=0.05, brk=0.02, box=0.01,
             dq=2.0, dq_conf=0.8):
    return {
        "product_match": SimpleNamespace(choice=pm, confidence=pm_conf),
        "is_accessory_only": SimpleNamespace(noul=acc),
        "is_broken_or_parts": SimpleNamespace(noul=brk),
        "is_empty_box_or_manual": SimpleNamespace(noul=box),
        "deal_quality": SimpleNamespace(score=dq, confidence=dq_conf),
    }


async def test_judge_notify_good_deal(monkeypatch, tmp_path):
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient",
                        _fake_client_factory(_answers()))
    j = deal_judge.DealJudge(api_key="test", model="jev-latest",
                             db_path=str(tmp_path / "t.db"))
    v = await j.judge("Nintendo 3DS", "Nintendo 3DS XL funzionante", 60.0,
                      market_price=100.0, threshold=70.0, url="http://x/1")
    assert v["decision"] == "notify"


async def test_judge_skip_accessory(monkeypatch, tmp_path):
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient",
                        _fake_client_factory(_answers(pm="accessory_only", pm_conf=0.9, acc=0.9)))
    j = deal_judge.DealJudge(api_key="test", model="jev-latest",
                             db_path=str(tmp_path / "t.db"))
    v = await j.judge("Nintendo 3DS", "custodia per 3DS", 8.0,
                      market_price=100.0, threshold=70.0, url="http://x/2")
    assert v["decision"] == "skip"


async def test_judge_skip_broken(monkeypatch, tmp_path):
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient",
                        _fake_client_factory(_answers(brk=0.95)))
    j = deal_judge.DealJudge(api_key="test", model="jev-latest",
                             db_path=str(tmp_path / "t.db"))
    v = await j.judge("Nintendo 3DS", "3DS rotto per pezzi", 20.0,
                      market_price=100.0, threshold=70.0, url="http://x/3")
    assert v["decision"] == "skip"


async def test_judge_review_when_uncertain(monkeypatch, tmp_path):
    import typesafe_sdk
    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient",
                        _fake_client_factory(_answers(pm_conf=0.3, dq_conf=0.3)))
    j = deal_judge.DealJudge(api_key="test", model="jev-latest",
                             db_path=str(tmp_path / "t.db"))
    v = await j.judge("Nintendo 3DS", "3DS ???", 60.0,
                      market_price=100.0, threshold=70.0, url="http://x/4")
    assert v["decision"] == "review"


async def test_judge_caches_same_url(monkeypatch, tmp_path):
    import typesafe_sdk
    calls = {"n": 0}

    class CountingClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def system_one(self, **k):
            calls["n"] += 1
            return SimpleNamespace(answers=_answers(), usage=None)

    monkeypatch.setattr(typesafe_sdk, "AsyncTypeSafeClient", CountingClient)
    j = deal_judge.DealJudge(api_key="test", model="jev-latest",
                             db_path=str(tmp_path / "t.db"))
    await j.judge("q", "t", 10.0, url="http://x/same")
    await j.judge("q", "t", 10.0, url="http://x/same")
    assert calls["n"] == 1


def test_get_judge_none_without_key(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    deal_judge.reset_judge_for_tests()
    assert deal_judge.get_judge() is None
    deal_judge.reset_judge_for_tests()
