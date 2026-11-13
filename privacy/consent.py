"""Согласия законного представителя: выдача, проверка, отзыв.

* Согласие оформляется отдельно на каждую цель (purpose) и фиксирует версию текста.
* Подпись — простая электронная подпись (код из SMS). Электронная форма с ЭП приравнивается
  к письменной (ст. 9 ч. 4 152-ФЗ). В document_ref храним хэш: текст + версия + код + время.
* Отзыв — одно действие. Gate сразу перестаёт принимать события этой цели, а накопленные
  по цели данные удаляются по плану PURGE_BY_PURPOSE не позже чем через 30 дней (ст. 21 ч. 5).
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from passport.store import Store

PURPOSES = ("passport", "activity", "recommendations", "share_partner", "export")
PURGE_DEADLINE = timedelta(days=30)

# Тексты согласий версионируются; родитель видит ровно ту версию, которую подписывает.
CONSENT_TEXTS = {
    ("passport", "v1"): "Согласие на ведение цифрового паспорта: участие в сменах и курсах, навыки, "
                        "достижения, отзывы наставников, проекты. Без данных о здоровье и геопозиции.",
    ("activity", "v1"): "Согласие на расчёт недельных уровней активности и режима сна по данным браслета. "
                        "В паспорт попадают только уровни (низкий/средний/высокий), без координат и пульса.",
    ("recommendations", "v1"): "Согласие на подбор смен, курсов и проектов по данным паспорта с объяснением причин.",
    ("share_partner", "v1"): "Согласие на передачу выбранных категорий паспорта указанному лагерю-партнёру.",
    ("export", "v1"): "Согласие на выпуск переносимых цифровых бейджей (Open Badges 3.0) и QR-кода для проверки.",
}

# Что удаляется при отзыве согласия на цель.
PURGE_BY_PURPOSE = {
    "activity": ["activity", "skills:activity.*"],
    "recommendations": ["recommendations"],
    "share_partner": ["partner_share", "notify_partner"],
    "export": ["revoke_exported_badges"],
    "passport": ["full_deletion"],       # без согласия на паспорт паспорта нет → полный процесс удаления
}


@dataclass
class ConsentService:
    store: Store

    def grant(self, child_id: str, guardian_sub: str, purpose: str, sms_code: str,
              text_version: str = "v1", data_categories: tuple[str, ...] = (),
              partner_issuer_id: Optional[str] = None, now: Optional[datetime] = None) -> dict:
        if purpose not in PURPOSES:
            raise ValueError(f"unknown purpose {purpose}")
        if (purpose, text_version) not in CONSENT_TEXTS:
            raise ValueError("нет такой версии текста согласия")
        if purpose == "share_partner" and (not partner_issuer_id or not data_categories):
            raise ValueError("для передачи партнёру нужно указать партнёра и категории данных")
        now = now or datetime.now(timezone.utc)
        text = CONSENT_TEXTS[(purpose, text_version)]
        document_ref = hashlib.sha256(f"{text}|{text_version}|{sms_code}|{now.isoformat()}".encode()).hexdigest()
        record = {
            "consent_id": str(uuid.uuid4()), "child_id": child_id, "guardian_sub": guardian_sub,
            "purpose": purpose, "data_categories": tuple(data_categories),
            "partner_issuer_id": partner_issuer_id, "text_version": text_version,
            "document_ref": document_ref, "granted_at": now, "revoked_at": None,
        }
        self.store.consents[child_id].append(record)
        self.store.outbox.append({"event_type": "ConsentGranted", "child_id": child_id, "purpose": purpose})
        return record

    def active(self, child_id: str, purpose: str, partner_issuer_id: Optional[str] = None) -> list[dict]:
        return [c for c in self.store.consents.get(child_id, [])
                if c["purpose"] == purpose and c["revoked_at"] is None
                and (partner_issuer_id is None or c["partner_issuer_id"] == partner_issuer_id)]

    def has(self, child_id: str, purpose: str, partner_issuer_id: Optional[str] = None) -> bool:
        return bool(self.active(child_id, purpose, partner_issuer_id))

    def revoke(self, child_id: str, consent_id: str, now: Optional[datetime] = None) -> dict:
        """Отзыв: помечает согласие и сразу выполняет план очистки по цели. Возвращает отчёт."""
        now = now or datetime.now(timezone.utc)
        rec = next(c for c in self.store.consents[child_id] if c["consent_id"] == consent_id)
        if rec["revoked_at"] is not None:
            return {"already_revoked": True}
        rec["revoked_at"] = now
        self.store.outbox.append({"event_type": "ConsentRevoked", "child_id": child_id,
                                  "purpose": rec["purpose"], "consent_id": consent_id})
        done = self._purge(child_id, rec)
        return {"purpose": rec["purpose"], "purged": done, "deadline": now + PURGE_DEADLINE}

    def _purge(self, child_id: str, rec: dict) -> list[str]:
        s, done = self.store, []
        for step in PURGE_BY_PURPOSE[rec["purpose"]]:
            if step == "activity":
                s.activity.pop(child_id, None)
            elif step == "skills:activity.*":
                s.skills[child_id] = [k for k in s.skills.get(child_id, []) if not k["skill_id"].startswith("activity.")]
            elif step == "recommendations":
                s.recommendations.pop(child_id, None)
            elif step == "partner_share":
                s.partner_shares[child_id].discard(rec["partner_issuer_id"])
            elif step == "notify_partner":
                s.outbox.append({"webhook": rec["partner_issuer_id"], "event_type": "ConsentRevoked",
                                 "child_id": child_id, "action": "delete_received_data"})
            elif step == "revoke_exported_badges":
                for a in s.achievements.get(child_id, []):
                    if a.get("credential_id") and a.get("exported"):
                        s.revoked_credentials[a["credential_id"]] = "consent_revoked"
            elif step == "full_deletion":
                s.outbox.append({"event_type": "DeletionRequested", "child_id": child_id,
                                 "reason": "passport consent revoked"})
            done.append(step)
        return done
