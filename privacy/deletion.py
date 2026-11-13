"""«Право родителя удалить»: полное удаление паспорта, аналитики и бэкапов.

Шаги выполняются по порядку, каждый отмечается в steps_done (как passport.deletion_request в DDL),
поэтому процесс можно продолжить после сбоя. Срок — 30 дней с запроса (ст. 21 ч. 5 152-ФЗ),
в прототипе всё выполняется сразу.

  1. blocked            — паспорт заблокирован: gate отклоняет новые события, API отдаёт 410
  2. badges_revoked     — выданные бейджи помечены отозванными в списке статусов (QR покажет «отозван»)
  3. projections_purged — удалены все проекции: навыки, достижения, проекты, наблюдения, активность…
  4. key_shredded       — уничтожен ключ ребёнка: журнал и все бэкапы становятся нечитаемыми
  5. analytics_purged   — удалены строки аналитики по псевдониму и сам псевдоним
  6. partners_notified  — партнёрам, которым передавались данные, отправлен webhook на удаление
  7. act_created        — сформирован акт об уничтожении (без ПДн: id запроса, даты, шаги)

Что остаётся и почему: бухгалтерские данные о бронированиях живут вне паспорта и хранятся
по своим срокам; запись deletion_request без ПДн нужна как доказательство исполнения.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from passport.store import PROJECTIONS, Store
from privacy.keys import KeyStore

STEPS = ("blocked", "badges_revoked", "projections_purged", "key_shredded",
         "analytics_purged", "partners_notified", "act_created")
DEADLINE = timedelta(days=30)


def pseudonym(secret: bytes, child_id: str) -> str:
    """Псевдоним для аналитики (ClickHouse): HMAC, без ключа обратно не восстановить."""
    return hmac.new(secret, child_id.encode(), hashlib.sha256).hexdigest()[:16]


@dataclass
class DeletionRequest:
    child_id: str
    guardian_sub: str
    requested_at: datetime
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    steps_done: list[str] = field(default_factory=list)
    completed_at: Optional[datetime] = None
    act: Optional[dict] = None

    @property
    def deadline_at(self) -> datetime:
        return self.requested_at + DEADLINE


@dataclass
class DeletionService:
    store: Store
    keys: KeyStore
    pseudonym_secret: bytes

    def request(self, child_id: str, guardian_sub: str, now: Optional[datetime] = None) -> DeletionRequest:
        return DeletionRequest(child_id, guardian_sub, now or datetime.now(timezone.utc))

    def execute(self, req: DeletionRequest, now: Optional[datetime] = None) -> DeletionRequest:
        now = now or datetime.now(timezone.utc)
        s, cid = self.store, req.child_id
        for step in STEPS:
            if step in req.steps_done:
                continue                           # идемпотентность: повторный запуск продолжает с места сбоя
            if step == "blocked":
                if cid in s.children:
                    s.children[cid]["status"] = "deletion_pending"
            elif step == "badges_revoked":
                for a in s.achievements.get(cid, []):
                    if a.get("credential_id"):
                        s.revoked_credentials[a["credential_id"]] = "deleted_by_guardian"
            elif step == "projections_purged":
                for table in PROJECTIONS:
                    getattr(s, table).pop(cid, None)
                for key in [k for k, v in s.external_ids.items() if v == cid]:
                    del s.external_ids[key]
            elif step == "key_shredded":
                self.keys.shred(cid)
                s.events.pop(cid, None)            # строки журнала удаляем; в бэкапах они остаются, но нечитаемы
            elif step == "analytics_purged":
                p = pseudonym(self.pseudonym_secret, cid)
                s.analytics[:] = [r for r in s.analytics if r.get("pseudonym") != p]
            elif step == "partners_notified":
                for partner in sorted(s.partner_shares.pop(cid, set())):
                    s.outbox.append({"webhook": partner, "event_type": "ChildDataDeletionRequested",
                                     "request_id": req.request_id})
            elif step == "act_created":
                s.consents.pop(cid, None)
                s.children.pop(cid, None)
                req.act = {"request_id": req.request_id, "requested_at": req.requested_at.isoformat(),
                           "completed_at": now.isoformat(), "steps": list(STEPS),
                           "within_deadline": now <= req.deadline_at}
            req.steps_done.append(step)
        req.completed_at = now
        return req
