"""Проверки шагов 1–3: каталог, таксономия, события, правила согласованы между собой."""
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from passport.events import PAYLOADS, REQUIRED_CONSENT, Envelope, validate

ROOT = Path(__file__).resolve().parents[1]
SKILLS = {k for c in yaml.safe_load((ROOT / "data/skills.yaml").read_text())["categories"].values() for k in c["skills"]}
CATALOG = json.loads((ROOT / "data/catalog.json").read_text())
RULES = yaml.safe_load((ROOT / "passport/rules.yaml").read_text())


def test_catalog_tags_exist_in_taxonomy():
    assert all(t in SKILLS for p in CATALOG["programs"] for t in p["skill_tags"])


def test_every_event_has_consent_rule():
    assert set(PAYLOADS) == set(REQUIRED_CONSENT)


def test_rules_cover_only_known_events():
    assert set(RULES["evidence"]) <= set(PAYLOADS)


def test_levels_3_4_need_mentor():
    assert RULES["levels"][3]["requires_mentor_confirmation"] == 3
    assert RULES["levels"][4]["requires_mentor_confirmation"] == 4


def test_valid_envelope_passes():
    env = Envelope(event_type="TestPassed", source="online_school", child_ref="school-42",
                   occurred_at=datetime.now(timezone.utc),
                   payload={"test_id": "t1", "course_id": "course.python-start", "score_pct": 85})
    assert validate(env).score_pct == 85


def test_bad_payload_rejected():
    env = Envelope(event_type="TestPassed", source="online_school", child_ref="school-42",
                   occurred_at=datetime.now(timezone.utc), payload={"test_id": "t1", "score_pct": 140})
    with pytest.raises(ValueError):
        validate(env)


def test_unknown_event_rejected():
    env = Envelope(event_type="HeartRateSample", source="telemetry", child_ref="x",
                   occurred_at=datetime.now(timezone.utc), payload={})
    with pytest.raises(ValueError, match="unknown"):
        validate(env)
