"""Шаг 10 ноября: генератор → сервис паспорта → проекции; агрегатор телеметрии."""
import copy
import json
from datetime import date, datetime, timezone

import pytest

from generator.generate import build, load
from passport.events import Envelope
from privacy.deletion import DeletionService
from telemetry.aggregate import aggregate
from telemetry.simulate import simulate


@pytest.fixture(scope="module")
def run():
    world = build(n=60, seed=3)
    svc, info = load(world)
    return world, svc, info


def test_every_event_is_accounted_for(run):
    world, svc, info = run
    assert sum(info["results"].values()) == len(world.events)
    assert info["results"]["duplicate"] == 1
    assert info["results"]["rejected: identity"] == 1
    assert info["results"]["rejected: schema"] == 1


def test_dlq_keeps_no_payload(run):
    _, svc, _ = run
    assert svc.dlq and all("payload" not in d for d in svc.dlq)


def test_journal_is_encrypted(run):
    _, svc, info = run
    rec = svc.store.events[info["hero_id"]][0]
    assert b"course" not in rec["payload_enc"]
    assert json.loads(svc.keys.decrypt(info["hero_id"], rec["payload_enc"]))


def test_consent_gate_blocks_activity_without_consent(run):
    world, svc, _ = run
    for cid, child in svc.store.children.items():
        if not svc.consents.has(cid, "activity"):
            assert not svc.store.activity.get(cid)
            # activity.* из смен (выносливость, плавание) законны под согласием passport;
            # без согласия activity в журнал не должна попасть ни одна сводка браслета
            assert not any(e["event_type"] == "ActivityWeeklySummarized"
                           for e in svc.store.events.get(cid, []))
        if not svc.consents.has(cid, "passport"):
            assert not svc.store.events.get(cid) or all(
                e["event_type"] == "ActivityWeeklySummarized" for e in svc.store.events[cid])


def test_levels_3_plus_always_mentor_confirmed(run):
    _, svc, _ = run
    for cid, rows in svc.store.skills.items():
        for r in rows:
            if r["level"] >= 3:
                assert r["max_trust_level"] >= 2
    for rows in svc.store.achievements.values():
        for a in rows:
            if a["type"] == "skill_level":
                assert a["signer_id"] and a["issuer_id"]


def test_activity_skills_capped_at_2(run):
    _, svc, _ = run
    assert all(r["level"] <= 2 for rows in svc.store.skills.values() for r in rows
               if r["skill_id"].startswith("activity."))


def test_hero_passport(run):
    _, svc, info = run
    hid = info["hero_id"]
    skills = {r["skill_id"]: r["level"] for r in svc.current_skills(hid)}
    assert skills["tech.python"] == 3
    titles = [a["title"] for a in svc.store.achievements[hid]]
    assert "Программирование на Python: уровень 3" in titles
    assert any(a["issuer_name"].startswith("Лагерь «Сосновый берег»") for a in svc.store.achievements[hid])
    # граничный случай: достижение партнёра за смену, на которой Маши не было, не принято
    assert "Турист года" not in titles


def test_skill_history_scd2(run):
    _, svc, info = run
    hid = info["hero_id"]
    assert svc.skill_level_at(hid, "tech.python", datetime(2026, 10, 1, tzinfo=timezone.utc)) == 0
    assert svc.skill_level_at(hid, "tech.python", datetime(2026, 12, 31, tzinfo=timezone.utc)) == 2
    assert svc.skill_level_at(hid, "tech.python", datetime(2027, 8, 31, tzinfo=timezone.utc)) == 3


def test_rebuild_is_deterministic_and_follows_rules(run):
    _, svc, info = run
    hid = info["hero_id"]
    before = [(r["skill_id"], r["level"]) for r in svc.current_skills(hid)]
    svc.rebuild(hid)
    assert [(r["skill_id"], r["level"]) for r in svc.current_skills(hid)] == before
    # меняем правила задним числом: уровень 2 теперь с 20 очков → робототехника/электроника падают
    old_rules = svc.rules
    svc.rules = copy.deepcopy(old_rules)
    svc.rules["levels"][2]["min_points"] = 20
    svc.rebuild(hid)
    assert {r["skill_id"]: r["level"] for r in svc.current_skills(hid)}["tech.electronics"] == 1
    svc.rules = old_rules
    svc.rebuild(hid)


def test_after_deletion_new_events_are_rejected():
    world = build(n=5, seed=9)
    svc, info = load(world)
    hid = info["hero_id"]
    deleter = DeletionService(svc.store, svc.keys, b"s")
    deleter.execute(deleter.request(hid, "kc-parent-masha"))
    r = svc.ingest(Envelope(event_type="LessonCompleted", source="online_school", child_ref="sch-masha",
                            occurred_at=datetime.now(timezone.utc),
                            payload={"lesson_id": "l", "course_id": "course.python-start", "duration_min": 45}))
    assert r.status == "rejected" and r.reason.startswith("identity")


# ── Агрегатор телеметрии ──────────────────────────────────────

def test_aggregate_levels_and_no_raw_fields():
    active = list(simulate("a", date(2027, 6, 7), 7, 140, 22.0, 10, seed=1))
    lazy = list(simulate("b", date(2027, 6, 7), 7, 20, 23.0, 120, seed=2))
    out = {e.child_ref: e.payload for e in aggregate(active + lazy)}
    assert out["a"]["activity_level"] == "high" and out["a"]["regularity"] == "very_regular"
    assert out["a"]["sleep_regularity"] == "regular"
    assert out["b"]["activity_level"] == "low" and out["b"]["regularity"] == "irregular"
    assert out["b"]["sleep_regularity"] == "irregular"
    for p in out.values():
        assert set(p) == {"week_start", "activity_level", "regularity", "sleep_regularity"}


def test_aggregate_skips_weeks_with_too_little_data():
    two_days = list(simulate("c", date(2027, 6, 7), 2, 140, 22.0, 10, seed=3))
    assert aggregate(two_days) == []
