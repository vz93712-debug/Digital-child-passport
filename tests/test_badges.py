"""Шаг 11 ноября: Open Badges 3.0, проверка подписи, L3 для партнёра, отзыв."""
import json
from datetime import datetime, timezone

import pytest

from generator.generate import P1, P2, build, load, partner_signer
from verify.badges import (BadgeService, IssuerKey, IssuerRegistry, Verifier, b64u, build_credential, decode,
                           public_from_multibase, public_multibase, sign)

NOW = datetime(2027, 7, 25, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def run():
    registry, keys = IssuerRegistry.demo()
    verifier = Verifier(registry)
    world = build(n=40, seed=5, sign=partner_signer(keys[P1]),
                  forge=partner_signer(IssuerKey.demo(P1, salt="attacker")))
    svc, info = load(world, verifier=verifier.partner_hook())
    verifier.revoked = svc.store.revoked_credentials
    return registry, keys, verifier, svc, info


def test_registry_rule_tier3_needs_key():
    registry, keys = IssuerRegistry.demo()
    assert registry.issuers[P1]["public_key"] and not registry.issuers[P2]["public_key"]
    registry.issuers[P1]["public_key"] = None
    with pytest.raises(ValueError):
        registry.check()


def test_multibase_roundtrip():
    k = IssuerKey.demo(P1)
    mb = k.public_multibase
    assert mb.startswith("z6Mk")                      # префикс did:key для Ed25519
    assert public_multibase(public_from_multibase(mb)) == mb


def test_partner_badge_verified_gets_l3_and_forgery_goes_to_dlq(run):
    _, _, verifier, svc, info = run
    a = next(x for x in svc.store.achievements[info["hero_id"]] if x["issuer_id"] == P1)
    assert a["trust_level"] == 3 and a["credential_jws"]
    assert verifier.verify(a["credential_jws"], NOW)["status"] == "valid"
    assert any(d["kind"] == "credential" for d in svc.dlq)
    assert not any("инструктор" in x["title"] for x in svc.store.achievements[info["hero_id"]])


def test_without_verifier_partner_badge_is_only_l1():
    registry, keys = IssuerRegistry.demo()
    svc, info = load(build(n=5, seed=5, sign=partner_signer(keys[P1])))
    a = next(x for x in svc.store.achievements[info["hero_id"]] if x["issuer_id"] == P1)
    assert a["trust_level"] == 1


def test_partner_hook_rejects_mismatch(run):
    registry, keys, verifier, _, _ = run
    hook = verifier.partner_hook()
    issuer = registry.issuers[P1]
    cred = build_credential(issuer, "Пилот", "—", "award", ["tech.drones"], "x", NOW)
    jws = sign(cred, keys[P1])
    payload = {"title": "Пилот", "skill_ids": ["tech.drones"]}
    assert hook(jws, P1, payload)
    assert not hook(jws, P1, payload | {"title": "Пилот-ас"})            # событие говорит одно, бейдж другое
    assert not hook(jws, P2, payload)                                    # прислал не тот, кто подписал
    camp = "00000000-0000-4000-8000-000000000001"
    own = sign(build_credential(registry.issuers[camp], "Пилот", "—", "award", ["tech.drones"], "x", NOW), keys[camp])
    assert not hook(own, camp, payload)                                  # L3 только у партнёров уровня 3


def test_export_requires_consent_and_hides_child_identity(run):
    _, keys, verifier, svc, info = run
    cid = info["hero_id"]
    a = next(x for x in svc.store.achievements[cid] if x["type"] == "skill_level")
    exp = BadgeService(svc, keys).export(cid, a["achievement_id"], NOW)
    header, cred = decode(exp["jws"])
    assert header["alg"] == "EdDSA" and "OpenBadgeCredential" in cred["type"]
    text = json.dumps(cred, ensure_ascii=False)
    assert cid not in text and "Маша" not in text
    assert verifier.verify(exp["jws"], NOW)["status"] == "valid"
    other = next(c for c in svc.store.children if not svc.consents.has(c, "export")
                 and svc.store.achievements.get(c))
    with pytest.raises(PermissionError):
        BadgeService(svc, keys).export(other, svc.store.achievements[other][0]["achievement_id"])


def test_tampering_and_alg_none_are_invalid(run):
    registry, keys, verifier, _, _ = run
    cred = build_credential(registry.issuers[P1], "Пилот", "—", "award", ["tech.drones"], "x", NOW)
    h, p, s = sign(cred, keys[P1]).split(".")
    changed = b64u(json.dumps(dict(cred, name="Пилот-ас"), ensure_ascii=False).encode())
    assert verifier.verify(f"{h}.{changed}.{s}", NOW)["status"] == "invalid"
    none_header = b64u(json.dumps({"alg": "none", "kid": "x"}).encode())
    assert verifier.verify(f"{none_header}.{p}.", NOW)["status"] == "invalid"
    assert verifier.verify("not-a-jws", NOW)["status"] == "invalid"


def test_revocation_and_issuer_suspension(run):
    registry, keys, verifier, _, _ = run
    cred = build_credential(registry.issuers[P1], "Пилот", "—", "award", ["tech.drones"], "x", NOW)
    jws = sign(cred, keys[P1])
    verifier.revoked[cred["id"]] = "deleted_by_guardian"
    r = verifier.verify(jws, NOW)
    assert r["status"] == "revoked" and r["checks"]["signature"]
    del verifier.revoked[cred["id"]]
    registry.issuers[P1]["status"] = "suspended"
    try:
        assert verifier.verify(jws, NOW)["status"] == "issuer_suspended"
    finally:
        registry.issuers[P1]["status"] = "active"


def test_rebuild_keeps_l3(run):
    _, _, _, svc, info = run
    cid = info["hero_id"]
    before = sorted((a["title"], a["trust_level"]) for a in svc.store.achievements[cid])
    svc.rebuild(cid)
    assert sorted((a["title"], a["trust_level"]) for a in svc.store.achievements[cid]) == before
