"""Рекомендатель v1: правила, объяснения, согласие."""
from collections import Counter
from datetime import date

import pytest

from generator.generate import build, load
from recommender.engine import MAX_PER_KIND, WEIGHTS, recommend

TODAY = date(2027, 7, 25)


@pytest.fixture(scope="module")
def run():
    svc, info = load(build(n=80, seed=11))
    return svc, info


def test_weights_sum_to_one():
    assert abs(sum(WEIGHTS.values()) - 1) < 1e-9


def test_hard_rules_hold_for_everyone(run):
    svc, _ = run
    for cid, child in svc.store.children.items():
        items = recommend(svc, cid, TODAY, save=False)["items"]
        age = TODAY.year - child["birth_year"]
        done = {p["program_id"] for p in svc.store.participations[cid] if p["status"] == "completed"}
        for r in items:
            prog = svc.programs[r.program_id]
            assert prog["age_min"] <= age <= prog["age_max"]
            assert r.program_id not in done
            assert not prog.get("starts_on") or date.fromisoformat(prog["starts_on"]) >= TODAY
            assert r.reasons, "каждая рекомендация объяснена"
        kinds = Counter(r.kind for r in items if r.rule is None)
        assert all(v <= MAX_PER_KIND for v in kinds.values())


def test_no_consent_no_recommendations(run):
    svc, _ = run
    cid = next(c for c in svc.store.children if not svc.consents.has(c, "recommendations"))
    assert recommend(svc, cid, TODAY)["items"] == []
    assert not svc.store.recommendations.get(cid)


def test_hero_gets_growth_step_and_saved_rows(run):
    svc, info = run
    cid = info["hero_id"]
    items = recommend(svc, cid, TODAY)["items"]
    assert items and items[0].components["growth"] > 0
    assert all(r.program_id != "course.python-start" for r in items)      # уже пройден
    rows = svc.store.recommendations[cid]
    assert rows[0]["model_version"] == "rules-v1" and rows[0]["reasons"]


def test_unfinished_course_goes_first(run):
    svc, _ = run
    for cid in svc.store.children:
        if not svc.consents.has(cid, "recommendations"):
            continue
        active = [p for p in svc.store.participations[cid]
                  if p["status"] == "active" and svc.programs[p["program_id"]]["kind"] == "course"]
        if active:
            items = recommend(svc, cid, TODAY, save=False)["items"]
            assert items[0].rule == "unfinished_course"
            return
    pytest.skip("в выборке нет незаконченных курсов")
