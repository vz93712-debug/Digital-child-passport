"""Цифровые бейджи: Open Badges 3.0 с подписью Ed25519, проверка и QR-код.

Источник истины — запись достижения в БД паспорта. Бейдж — переносимая подписанная копия:
  * выпускается по запросу родителя (нужно согласие «export») или приходит от партнёра;
  * формат — OpenBadgeCredential (модель Verifiable Credentials 2.0), защищённый как VC-JWT:
    компактный JWS, alg EdDSA (Ed25519), kid = DID эмитента + «#key-1»;
  * ребёнок в бейдже не назван: credentialSubject.identifier — солёный SHA-256 от child_id
    (IdentityObject, hashed=true). Имя показывает только страница проверки, и только имя без фамилии;
  * статус бейджа — credentialStatus со ссылкой на список отзыва; отзыв не трогает подпись.

Упрощения прототипа (в проде иначе):
  * закрытые ключи эмитентов SportZania хранятся в KMS/HSM, здесь — в памяти, а ключи
    выводятся из детерминированного seed, чтобы демо было воспроизводимым;
  * DID эмитента не резолвится по сети: открытые ключи берутся из реестра эмитентов (таблица
    passport.issuer, столбец public_key в формате multibase);
  * JSON-LD-контекст не загружается и не канонизируется — подписывается JSON как есть (VC-JWT это допускает).
"""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from passport.service import load_catalog

VERIFY_BASE = "https://passport.sportzania.example/v1/credentials"
STATUS_LIST = "https://passport.sportzania.example/v1/status/revocations"
CONTEXTS = ["https://www.w3.org/ns/credentials/v2",
            "https://purl.imsglobal.org/spec/ob/v3p0/context-3.0.3.json"]
ACHIEVEMENT_TYPE = {"course_completion": "Course", "shift_completion": "Achievement", "project": "Project",
                    "skill_level": "Competency", "award": "Award"}


# ── Кодировки ─────────────────────────────────────────────────

def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _b58(data: bytes) -> str:
    n, out = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def _b58_decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def public_multibase(pub: Ed25519PublicKey) -> str:
    """Формат did:key / Multikey: 'z' + base58btc(0xED 0x01 + 32 байта ключа)."""
    raw = pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "z" + _b58(b"\xed\x01" + raw)


def public_from_multibase(mb: str) -> Ed25519PublicKey:
    if not mb.startswith("z"):
        raise ValueError("ожидается multibase base58btc")
    raw = _b58_decode(mb[1:])
    if raw[:2] != b"\xed\x01" or len(raw) != 34:
        raise ValueError("это не ключ Ed25519")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


def did_of(issuer_id: str) -> str:
    return f"did:web:passport.sportzania.example:issuers:{issuer_id}"


def issuer_id_of(did: str) -> str:
    return did.rsplit(":", 1)[-1]


# ── Ключи и реестр эмитентов ──────────────────────────────────

@dataclass
class IssuerKey:
    issuer_id: str
    private: Ed25519PrivateKey

    @classmethod
    def demo(cls, issuer_id: str, salt: str = "sk-kids-demo") -> "IssuerKey":
        """Детерминированный ключ для демо. Только для прототипа!"""
        seed = hashlib.sha256(f"{salt}:{issuer_id}".encode()).digest()
        return cls(issuer_id, Ed25519PrivateKey.from_private_bytes(seed))

    @property
    def kid(self) -> str:
        return did_of(self.issuer_id) + "#key-1"

    @property
    def public_multibase(self) -> str:
        return public_multibase(self.private.public_key())


@dataclass
class IssuerRegistry:
    """Копия таблицы passport.issuer: имя, уровень доверия, статус, открытый ключ."""
    issuers: dict[str, dict]

    @classmethod
    def demo(cls) -> tuple["IssuerRegistry", dict[str, IssuerKey]]:
        """Реестр по каталогу + демо-ключи для эмитентов SportZania и партнёра уровня 3."""
        issuers, _ = load_catalog()
        reg, keys = {}, {}
        for iid, i in issuers.items():
            rec = dict(i, status="active", public_key=None)
            if i["trust_tier"] >= 2:
                keys[iid] = IssuerKey.demo(iid)
                rec["public_key"] = keys[iid].public_multibase
            reg[iid] = rec
        r = cls(reg)
        r.check()
        return r, keys

    def check(self) -> None:
        # то же правило, что CHECK (trust_tier < 3 OR public_key IS NOT NULL) в DDL
        bad = [i for i, r in self.issuers.items() if r["trust_tier"] >= 3 and not r.get("public_key")]
        if bad:
            raise ValueError(f"эмитенты уровня 3 без открытого ключа: {bad}")


# ── Выпуск ────────────────────────────────────────────────────

def holder_identifier(child_id: str, salt: str) -> dict:
    return {"type": "IdentityObject", "identityType": "identifier", "hashed": True, "salt": salt,
            "identityHash": "sha256$" + hashlib.sha256((child_id + salt).encode()).hexdigest()}


def build_credential(issuer: dict, title: str, criteria: str, atype: str, skill_ids: list[str],
                     holder_ref: str, issued_at: datetime, credential_id: Optional[str] = None,
                     salt: Optional[str] = None) -> dict:
    """OpenBadgeCredential. holder_ref — id ребёнка у эмитента (child_id или child_ref партнёра)."""
    cid = credential_id or f"urn:uuid:{uuid.uuid4()}"
    salt = salt or hashlib.sha256(cid.encode()).hexdigest()[:16]
    index = int(hashlib.sha256(cid.encode()).hexdigest(), 16) % 131072   # 16 КБ-битовая строка
    return {
        "@context": CONTEXTS,
        "id": cid,
        "type": ["VerifiableCredential", "OpenBadgeCredential"],
        "issuer": {"id": did_of(issuer["issuer_id"]), "type": ["Profile"], "name": issuer["name"]},
        "validFrom": issued_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "name": title,
        "credentialSubject": {
            "type": ["AchievementSubject"],
            "identifier": [holder_identifier(holder_ref, salt)],
            "achievement": {
                "id": f"{did_of(issuer['issuer_id'])}/achievements/{hashlib.sha1(title.encode()).hexdigest()[:12]}",
                "type": ["Achievement"],
                "achievementType": ACHIEVEMENT_TYPE.get(atype, "Achievement"),
                "name": title,
                "criteria": {"narrative": criteria},
                "tag": skill_ids,
            },
        },
        # W3C Bitstring Status List: бит с этим номером в списке отзыва = бейдж отозван.
        # В прототипе список отзыва — словарь Store.revoked_credentials по id бейджа.
        "credentialStatus": {"id": f"{STATUS_LIST}#{index}", "type": "BitstringStatusListEntry",
                             "statusPurpose": "revocation", "statusListIndex": str(index),
                             "statusListCredential": STATUS_LIST},
    }


def sign(credential: dict, key: IssuerKey) -> str:
    """VC-JWT: base64url(header).base64url(credential).base64url(Ed25519-подпись)."""
    if issuer_id_of(credential["issuer"]["id"]) != key.issuer_id:
        raise ValueError("ключ не принадлежит эмитенту бейджа")
    header = {"alg": "EdDSA", "typ": "vc+jwt", "kid": key.kid}
    signing_input = b64u(json.dumps(header, separators=(",", ":")).encode()) + "." + \
        b64u(json.dumps(credential, ensure_ascii=False, separators=(",", ":")).encode())
    return signing_input + "." + b64u(key.private.sign(signing_input.encode()))


def decode(jws: str) -> tuple[dict, dict]:
    """Разбирает JWS без проверки подписи (для поиска эмитента и id)."""
    h, p, _ = jws.split(".")
    return json.loads(b64u_decode(h)), json.loads(b64u_decode(p))


# ── Проверка ──────────────────────────────────────────────────

@dataclass
class Verifier:
    """Проверка бейджа: подпись, статус эмитента, список отзыва. Ответ — VerificationResult из OpenAPI."""
    registry: IssuerRegistry
    revoked: dict[str, str] = field(default_factory=dict)            # Store.revoked_credentials
    holder_name: Callable[[str], Optional[str]] = lambda credential_id: None

    def verify(self, jws: str, now: Optional[datetime] = None) -> dict:
        now = now or datetime.now(timezone.utc)
        checks = {"signature": False, "issuer_active": False, "not_revoked": False}
        try:
            header, cred = decode(jws)
            h, p, s = jws.split(".")
        except Exception:
            return {"status": "invalid", "reason": "не JWS", "checks": checks}
        out = {"title": cred.get("name"), "issued_at": str(cred.get("validFrom", ""))[:10], "checks": checks}
        if header.get("alg") != "EdDSA":
            return out | {"status": "invalid", "reason": f"алгоритм {header.get('alg')} не принимается"}
        iid = issuer_id_of(cred.get("issuer", {}).get("id", ""))
        issuer = self.registry.issuers.get(iid)
        if issuer is None or header.get("kid", "").split("#")[0] != did_of(iid):
            return out | {"status": "invalid", "reason": "эмитент неизвестен или kid не его"}
        out["issuer_name"] = issuer["name"]
        if not issuer.get("public_key"):
            return out | {"status": "invalid", "reason": "у эмитента нет открытого ключа"}
        try:
            public_from_multibase(issuer["public_key"]).verify(b64u_decode(s), f"{h}.{p}".encode())
            checks["signature"] = True
        except (InvalidSignature, ValueError):
            return out | {"status": "invalid", "reason": "подпись не сходится: бейдж изменён или подделан"}
        valid_until = cred.get("validUntil")
        if valid_until and datetime.fromisoformat(valid_until.replace("Z", "+00:00")) < now:
            return out | {"status": "invalid", "reason": "срок действия истёк"}
        checks["issuer_active"] = issuer.get("status") == "active"
        checks["not_revoked"] = cred["id"] not in self.revoked
        name = self.holder_name(cred["id"])
        if name:
            out["holder_display_name"] = name
        if not checks["issuer_active"]:
            return out | {"status": "issuer_suspended", "reason": "эмитент приостановлен в реестре"}
        if not checks["not_revoked"]:
            return out | {"status": "revoked", "reason": self.revoked[cred["id"]]}
        return out | {"status": "valid", "credential_id": cred["id"], "issuer_id": iid}

    def partner_hook(self) -> Callable[[str, str, dict], bool]:
        """Хук для PassportService: бейдж партнёра даёт L3, только если он действителен,
        подписан тем же партнёром, что прислал событие, партнёр уровня 3, а содержание совпадает с событием."""
        def check(jws: str, issuer_id: str, payload: dict) -> bool:
            r = self.verify(jws)
            if r["status"] != "valid" or r["issuer_id"] != issuer_id:
                return False
            if self.registry.issuers[issuer_id]["trust_tier"] < 3:
                return False
            _, cred = decode(jws)
            ach = cred["credentialSubject"]["achievement"]
            return ach["name"] == payload["title"] and sorted(ach.get("tag", [])) == sorted(payload["skill_ids"])
        return check


def verify_url(credential_id: str) -> str:
    return f"{VERIFY_BASE}/{credential_id}/verify"


def qr_png(url: str, path: Path) -> Path:
    import qrcode
    path.parent.mkdir(parents=True, exist_ok=True)
    qrcode.make(url, box_size=6, border=2).save(path)
    return path


# ── Экспорт из паспорта ───────────────────────────────────────

@dataclass
class BadgeService:
    """Выпуск бейджа из записи достижения. Партнёрский бейдж отдаётся как есть — с подписью партнёра."""
    svc: "object"                       # PassportService
    keys: dict[str, IssuerKey]

    def export(self, child_id: str, achievement_id: str, now: Optional[datetime] = None) -> dict:
        if not self.svc.consents.has(child_id, "export"):
            raise PermissionError("нет согласия «export»: бейдж не выпускается")
        a = next(x for x in self.svc.store.achievements[child_id] if x["achievement_id"] == achievement_id)
        if a.get("revoked_at"):
            raise ValueError("достижение отозвано")
        if not a.get("credential_jws"):
            key = self.keys.get(a["issuer_id"] or "")
            if key is None:
                raise ValueError("эмитент не выпускает подписанные бейджи (нет ключа)")
            cred = build_credential(self.svc.issuers[a["issuer_id"]], a["title"], a["criteria"], a["type"],
                                    a["skill_ids"], child_id, datetime.fromisoformat(a["issued_at"]))
            a["credential_jws"], a["credential_id"] = sign(cred, key), cred["id"]
        a["exported"] = True
        self.svc.store.outbox.append({"event_type": "BadgeExported", "child_id": child_id,
                                      "credential_id": a["credential_id"]})
        return {"credential_id": a["credential_id"], "jws": a["credential_jws"],
                "verify_url": verify_url(a["credential_id"])}

    def holder_name(self, credential_id: str) -> Optional[str]:
        for cid, rows in self.svc.store.achievements.items():
            if any(r.get("credential_id") == credential_id for r in rows):
                return self.svc.store.children.get(cid, {}).get("display_name")
        return None
