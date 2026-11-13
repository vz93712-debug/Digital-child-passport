"""Согласия, отзыв и полное удаление с crypto-shredding."""
import json
from datetime import datetime, timedelta, timezone

import pytest

from passport.store import Store
from privacy.consent import ConsentService
from privacy.deletion import STEPS, DeletionService, pseudonym
from privacy.keys import KeyShredded, KeyStore

SECRET = b"analytics-secret"
NOW = datetime(2027, 6, 10, tzinfo=timezone.utc)


@pytest.fixture
def world():
    store, keys = Store(), KeyStore()
    cid = "masha"
    keys.create(cid)
    store.children[cid] = {"child_id": cid, "status": "active"}
    store.external_ids[("online_school", "s-42")] = cid
    store.events[cid].append({"event_id": "e1", "payload_enc": keys.encrypt(cid, json.dumps({"course": "python"}).encode())})
    store.skills[cid] = [{"skill_id": "tech.python", "level": 2}, {"skill_id": "activity.regularity", "level": 1}]
    store.activity[cid] = [{"week_start": "2027-06-07", "activity_level": "high"}]
    store.achievements[cid] = [{"achievement_id": "a1", "credential_id": "urn:uuid:c1", "exported": True}]
    store.recommendations[cid] = [{"program_id": "shift.robots-ai"}]
    store.analytics.append({"pseudonym": pseudonym(SECRET, cid), "event_type": "LessonCompleted"})
    store.analytics.append({"pseudonym": pseudonym(SECRET, "petya"), "event_type": "LessonCompleted"})
    store.partner_shares[cid].add("issuer-partner")
    return store, keys, cid


def test_consent_is_per_purpose_and_versioned(world):
    store, _, cid = world
    svc = ConsentService(store)
    c = svc.grant(cid, "kc-parent", "activity", sms_code="1234", now=NOW)
    assert svc.has(cid, "activity") and not svc.has(cid, "recommendations")
    assert c["text_version"] == "v1" and len(c["document_ref"]) == 64
    with pytest.raises(ValueError):
        svc.grant(cid, "kc-parent", "share_partner", sms_code="1")   # без партнёра и категорий


def test_revoking_activity_purges_only_activity(world):
    store, _, cid = world
    svc = ConsentService(store)
    c = svc.grant(cid, "kc-parent", "activity", sms_code="1234", now=NOW)
    report = svc.revoke(cid, c["consent_id"], now=NOW)
    assert not svc.has(cid, "activity")
    assert store.activity.get(cid) is None
    assert [k["skill_id"] for k in store.skills[cid]] == ["tech.python"]   # остальные навыки на месте
    assert report["deadline"] == NOW + timedelta(days=30)


def test_revoking_export_revokes_published_badges(world):
    store, _, cid = world
    svc = ConsentService(store)
    c = svc.grant(cid, "kc-parent", "export", sms_code="1", now=NOW)
    svc.revoke(cid, c["consent_id"], now=NOW)
    assert store.revoked_credentials["urn:uuid:c1"] == "consent_revoked"


def test_full_deletion_including_backups(world):
    store, keys, cid = world
    backup = store.snapshot()                      # бэкап, сделанный ДО удаления
    svc = DeletionService(store, keys, SECRET)
    req = svc.execute(svc.request(cid, "kc-parent", now=NOW), now=NOW + timedelta(days=1))

    assert req.steps_done == list(STEPS) and req.act["within_deadline"]
    assert all(v == 0 for v in store.footprint(cid).values())
    assert cid not in store.children and ("online_school", "s-42") not in store.external_ids
    assert [r["pseudonym"] for r in store.analytics] == [pseudonym(SECRET, "petya")]   # чужое не тронуто
    assert store.revoked_credentials["urn:uuid:c1"] == "deleted_by_guardian"
    assert {"webhook": "issuer-partner", "event_type": "ChildDataDeletionRequested",
            "request_id": req.request_id} in store.outbox

    # Бэкап физически содержит шифртекст, но прочитать его больше нельзя
    blob = backup.events[cid][0]["payload_enc"]
    with pytest.raises(KeyShredded):
        keys.decrypt(cid, blob)
    # Ключ нельзя «пересоздать» для того же ребёнка
    with pytest.raises(KeyShredded):
        keys.create(cid)


def test_deletion_is_resumable(world):
    store, keys, cid = world
    svc = DeletionService(store, keys, SECRET)
    req = svc.request(cid, "kc-parent", now=NOW)
    req.steps_done = ["blocked", "badges_revoked"]       # сбой после второго шага
    svc.execute(req, now=NOW)
    assert req.steps_done == list(STEPS)


def test_ciphertext_bound_to_child(world):
    _, keys, cid = world
    keys.create("petya")
    blob = keys.encrypt(cid, b"secret")
    with pytest.raises(Exception):
        keys.decrypt("petya", blob)
