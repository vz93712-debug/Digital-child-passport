"""Сервис паспорта: приём событий и построение проекций.

Конвейер одного события (как в архитектуре, шаги 3–4):
  1. схема       — payload проверяется по своему типу (passport/events.py);
  2. дедупликация — повтор event_id игнорируется (RabbitMQ доставляет «хотя бы один раз»);
  3. склейка     — child_ref источника переводится в child_id через external_ids;
  4. статус      — заблокированный (удаляемый) паспорт событий не принимает;
  5. consent gate — без действующего согласия на цель события оно не сохраняется вообще;
  6. журнал      — payload шифруется ключом ребёнка и дописывается в журнал;
  7. проекции    — participations, skills (SCD2), achievements, observations, projects, activity.

Отклонённые события попадают в DLQ с причиной. Правила навыков читаются из rules.yaml;
`rebuild()` стирает проекции и проигрывает журнал заново — так меняют правила задним числом.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Callable, Optional

import yaml

from passport.events import REQUIRED_CONSENT, Envelope, validate
from passport.store import PROJECTIONS, Store
from privacy.consent import ConsentService
from privacy.keys import KeyStore

ROOT = Path(__file__).resolve().parents[1]

# Проверка подписи бейджа партнёра: (credential_jws, issuer_id источника, payload) → действителен ли.
# Реализация — verify.badges.Verifier.partner_hook(). Если хук не подключён, подпись
# проверить нечем → бейдж принимается со словами партнёра, уровень доверия L1.
CredentialVerifier = Callable[[str, str, dict], bool]


@dataclass
class IngestResult:
    status: str          # accepted | duplicate | rejected
    reason: str = ""
    child_id: Optional[str] = None


def load_catalog(path: Path = ROOT / "data/catalog.json") -> tuple[dict, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return ({i["issuer_id"]: i for i in data["issuers"]}, {p["program_id"]: p for p in data["programs"]})


def load_skills(path: Path = ROOT / "data/skills.yaml") -> dict[str, str]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {sid: s["name"] for c in data["categories"].values() for sid, s in c["skills"].items()}


@dataclass
class PassportService:
    store: Store
    keys: KeyStore
    consents: ConsentService
    rules: dict = field(default_factory=lambda: yaml.safe_load((ROOT / "passport/rules.yaml").read_text(encoding="utf-8")))
    credential_verifier: Optional[CredentialVerifier] = None
    dlq: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.issuers, self.programs = load_catalog()
        self.skill_names = load_skills()
        # Накопители доказательств: (child_id, skill_id) → points, count, max_trust, confirmed
        self._acc: dict[tuple[str, str], dict] = {}
        self._eid: Optional[str] = None      # id события, которое сейчас проецируется (для DLQ)

    # ── Регистрация и склейка личности ─────────────────────────

    def register_child(self, birth_year: int, display_name: str, interests: list[str],
                       external_ids: list[tuple[str, str]], now: Optional[datetime] = None) -> str:
        child_id = str(uuid.uuid4())
        age = (now or datetime.now(timezone.utc)).year - birth_year
        band = "4-9" if age <= 9 else "10-13" if age <= 13 else "14-17" if age <= 17 else "18+"
        self.keys.create(child_id)
        self.store.children[child_id] = {"child_id": child_id, "birth_year": birth_year, "age_band": band,
                                         "display_name": display_name, "interests": list(interests),
                                         "status": "active"}
        for source, ext in external_ids:
            self.link(child_id, source, ext)
        return child_id

    def link(self, child_id: str, source: str, external_id: str) -> None:
        key = (source, external_id)
        if key in self.store.external_ids and self.store.external_ids[key] != child_id:
            raise ValueError(f"{key} уже привязан к другому ребёнку")
        self.store.external_ids[key] = child_id

    # ── Приём события ──────────────────────────────────────────

    def ingest(self, env: Envelope) -> IngestResult:
        try:
            payload = validate(env)
        except Exception as e:                                       # 1. схема
            return self._reject(env, "schema", str(e).splitlines()[0])
        eid = str(env.event_id)
        if eid in self.store.seen_event_ids:                         # 2. дедупликация
            return IngestResult("duplicate", "event_id уже обработан")
        source = env.source
        child_id = self.store.external_ids.get((source, env.child_ref))   # 3. склейка
        if child_id is None:
            return self._reject(env, "identity", f"неизвестный child_ref {env.child_ref} в {source}")
        child = self.store.children.get(child_id)
        if child is None or child["status"] != "active":             # 4. статус
            return self._reject(env, "blocked", "паспорт заблокирован или удаляется", child_id)
        purpose = REQUIRED_CONSENT[env.event_type]                   # 5. consent gate
        if purpose is not None:
            partner = source.split(":", 1)[1] if source.startswith("partner:") else None
            if not self.consents.has(child_id, purpose, partner):
                return self._reject(env, "consent", f"нет согласия «{purpose}»", child_id)
        record = {"event_id": eid, "event_type": env.event_type, "schema_version": env.schema_version,
                  "source": source, "occurred_at": env.occurred_at,
                  "payload_enc": self.keys.encrypt(child_id, payload.model_dump_json().encode())}
        self.store.events[child_id].append(record)                   # 6. журнал
        self.store.seen_event_ids.add(eid)
        self._eid = eid
        self._project(child_id, env.event_type, source, env.occurred_at, payload.model_dump(mode="json"))  # 7.
        return IngestResult("accepted", child_id=child_id)

    def _reject(self, env: Envelope, kind: str, reason: str, child_id: Optional[str] = None) -> IngestResult:
        # В DLQ не кладём payload: он может содержать ПДн, а у DLQ нет ключа ребёнка.
        self.dlq.append({"event_id": str(env.event_id), "event_type": env.event_type,
                         "source": env.source, "kind": kind, "reason": reason})
        return IngestResult("rejected", f"{kind}: {reason}", child_id)

    # ── Пересборка из журнала ──────────────────────────────────

    def rebuild(self, child_id: str) -> None:
        """Стирает проекции ребёнка и проигрывает его журнал заново (например, после смены rules.yaml)."""
        for t in PROJECTIONS:
            if t != "visibility":
                getattr(self.store, t).pop(child_id, None)
        for k in [k for k in self._acc if k[0] == child_id]:
            del self._acc[k]
        for rec in sorted(self.store.events.get(child_id, []), key=lambda r: r["occurred_at"]):
            payload = json.loads(self.keys.decrypt(child_id, rec["payload_enc"]))
            self._eid = rec["event_id"]
            self._project(child_id, rec["event_type"], rec["source"], rec["occurred_at"], payload)

    # ── Проекции ───────────────────────────────────────────────

    def _issuer_of(self, source: str, program_id: Optional[str]) -> Optional[str]:
        if source.startswith("partner:"):
            return source.split(":", 1)[1]
        prog = self.programs.get(program_id or "")
        return prog["issuer_id"] if prog else None

    def _skills_of(self, program_id: Optional[str], explicit: Optional[list[str]] = None) -> list[str]:
        if explicit:
            return [s for s in explicit if s in self.skill_names]
        prog = self.programs.get(program_id or "")
        return list(prog["skill_tags"]) if prog else []

    def _participation(self, child_id: str, program_id: str, issuer_id: Optional[str], when: datetime,
                       status: str, end: Optional[str] = None) -> dict:
        for p in self.store.participations[child_id]:
            if p["program_id"] == program_id and p["status"] != "cancelled":
                if status == "completed" or p["status"] == "planned":
                    p["status"] = status
                if end:
                    p["ended_at"] = end
                return p
        p = {"participation_id": str(uuid.uuid4()), "program_id": program_id, "issuer_id": issuer_id,
             "started_at": when.date().isoformat(), "ended_at": end, "status": status}
        self.store.participations[child_id].append(p)
        return p

    def _project(self, cid: str, etype: str, source: str, at: datetime, p: dict) -> None:
        ev = self.rules["evidence"].get(etype, {})
        prog_id = p.get("program_id") or p.get("course_id")
        issuer = self._issuer_of(source, prog_id)
        prog = self.programs.get(prog_id or "", {})

        if etype == "BookingConfirmed":
            self._participation(cid, prog_id, issuer, at, "planned", p["ends_on"])
            child = self.store.children[cid]
            child["interests"] = sorted(set(child["interests"]) | set(prog.get("skill_tags", [])))
        elif etype == "LessonCompleted":
            self._participation(cid, prog_id, issuer, at, "active")
            self._evidence(cid, self._skills_of(prog_id), ev["points"], ev["trust"], at)
        elif etype == "TestPassed":
            if p["score_pct"] >= ev["min_score_pct"]:
                self._evidence(cid, self._skills_of(prog_id), p["score_pct"] / 25, ev["trust"], at)
        elif etype == "CourseCompleted":
            self._participation(cid, prog_id, issuer, at, "completed", at.date().isoformat())
            self._evidence(cid, self._skills_of(prog_id), ev["points"], ev["trust"], at)
            score = f" с итоговым результатом {p['final_score_pct']:.0f}%" if p.get("final_score_pct") else ""
            self._achieve(cid, "course_completion", f"Курс «{prog.get('title', prog_id)}» пройден",
                          f"Пройдены все уроки курса{score}", issuer, 1, self._skills_of(prog_id), at)
        elif etype == "ShiftCompleted":
            attendance = p["attended_days"] / max(p["total_days"], 1)
            self._participation(cid, prog_id, issuer, at, "completed", at.date().isoformat())
            if attendance >= ev["min_attendance"]:
                self._evidence(cid, self._skills_of(prog_id), ev["points"], ev["trust"], at)
                self._achieve(cid, "shift_completion", f"Участник: {prog.get('title', prog_id)}",
                              "Посещено не менее 80% дней смены", issuer, 1, self._skills_of(prog_id), at)
        elif etype == "MentorReviewAdded":
            part = self._participation(cid, prog_id, issuer, at, "active")
            skills = self._skills_of(prog_id, p["skill_ids"])
            self.store.observations[cid].append({
                "observation_id": p["review_id"], "author_sub": p["author_sub"],
                "participation_id": part["participation_id"], "skill_ids": skills,
                "text_enc": self.keys.encrypt(cid, p["text"].encode()), "signed_at": at.isoformat()})
            self._evidence(cid, skills, ev["points"], ev["trust"], at, confirm=p.get("confirms_level"),
                           signer=p["author_sub"], issuer=issuer)
        elif etype == "ProjectSubmitted":
            self._participation(cid, prog_id, issuer, at, "active")
            proj = next((x for x in self.store.projects[cid] if x["project_id"] == p["project_id"]), None)
            if proj is None:
                self.store.projects[cid].append({"project_id": p["project_id"], "title": p["title"],
                                                 "goal": p["goal"], "result_url": p.get("result_url"),
                                                 "program_id": prog_id, "role": p["role"], "status": "submitted"})
            self._evidence(cid, self._skills_of(prog_id), ev["points"], ev["trust"], at)
        elif etype == "ProjectReviewed":
            proj = next((x for x in self.store.projects[cid] if x["project_id"] == p["project_id"]), None)
            if proj is not None:
                proj["status"] = "reviewed" if p["accepted"] else "submitted"
            if p["accepted"]:
                skills = self._skills_of(prog_id, p["skill_ids"])
                self._participation(cid, prog_id, issuer, at, "completed", at.date().isoformat())
                self._evidence(cid, skills, ev["points"], ev["trust"], at, confirm=p.get("confirms_level"),
                               signer=p["mentor_sub"], issuer=issuer)
                title = proj["title"] if proj else p["project_id"]
                self._achieve(cid, "project", f"Проект «{title}»", "Проект защищён и принят наставником",
                              issuer, 2, skills, at, signer=p["mentor_sub"])
        elif etype == "ActivityWeeklySummarized":
            self.store.activity[cid] = [a for a in self.store.activity[cid] if a["week_start"] != p["week_start"]] + [p]
            field_of = {"activity.regularity": "regularity", "activity.sleep_routine": "sleep_regularity"}
            for skill_id, scale in ev["skills"].items():
                pts = scale.get(p.get(field_of[skill_id]), 0)
                if pts:
                    self._evidence(cid, [skill_id], pts, ev["trust"], at)
        elif etype == "PartnerParticipationReported":
            self._participation(cid, prog_id, issuer, at, p["status"], p.get("ends_on"))
            self.store.partner_shares[cid].add(issuer)
            if p["status"] == "completed":
                self._evidence(cid, self._skills_of(prog_id), ev["points"], self._partner_trust(issuer, None, p), at)
        elif etype == "PartnerAchievementSubmitted":
            self.store.partner_shares[cid].add(issuer)
            trust = self._partner_trust(issuer, p.get("credential_jws"), p)
            if trust is None:
                self.dlq.append({"event_id": self._eid, "event_type": etype, "source": source, "kind": "credential",
                                 "reason": "подпись бейджа не прошла проверку"})
                return
            was_there = any(x["program_id"] == prog_id for x in self.store.participations[cid])
            if not was_there:
                self.dlq.append({"event_id": self._eid, "event_type": etype, "source": source, "kind": "evidence",
                                 "reason": "ребёнок не был на этой смене партнёра"})
                return
            skills = self._skills_of(prog_id, p["skill_ids"])
            self._evidence(cid, skills, ev["points"], trust, at)
            a = self._achieve(cid, "award", p["title"], p["criteria"], issuer, trust, skills, at)
            if trust == 3:
                # храним бейдж партнёра как есть: при экспорте отдаём его с подписью партнёра
                from verify.badges import decode
                a["credential_jws"], a["credential_id"] = p["credential_jws"], decode(p["credential_jws"])[1]["id"]
        # ConsentGranted/Revoked, VisibilityChanged, DeletionRequested обрабатывают сервисы privacy/

    def _partner_trust(self, issuer_id: str, credential: Optional[str], payload: dict) -> Optional[int]:
        """L3 — подпись партнёра уровня 3 проверена; L1 — слово партнёра без подписи;
        None — подпись есть, но не сходится (событие уходит в DLQ, достижение не создаётся)."""
        if not credential or self.credential_verifier is None:
            return 1
        if self.credential_verifier(credential, issuer_id, payload):
            return 3
        return None

    # ── Навыки: очки доказательства → уровень (SCD2) ───────────

    def _evidence(self, cid: str, skills: list[str], points: float, trust: int, at: datetime,
                  confirm: Optional[int] = None, signer: Optional[str] = None, issuer: Optional[str] = None) -> None:
        for sid in skills:
            acc = self._acc.setdefault((cid, sid), {"points": 0.0, "count": 0, "max_trust": 0, "confirmed": 0})
            acc["points"] += points
            acc["count"] += 1
            acc["max_trust"] = max(acc["max_trust"], trust)
            if confirm and trust >= 2 and confirm >= acc["confirmed"]:
                # запоминаем, кто подтвердил: достижение уровня подпишет именно он,
                # даже если порог очков добьёт более позднее системное событие
                acc.update(confirmed=confirm, confirmed_by=signer, confirmed_issuer=issuer)
            self._update_level(cid, sid, acc, at)

    def _level_for(self, sid: str, acc: dict) -> int:
        level = 0
        for lvl, cond in sorted(self.rules["levels"].items()):
            if acc["points"] < cond["min_points"]:
                break
            need = cond.get("requires_mentor_confirmation")
            if need and acc["confirmed"] < need:
                break
            level = lvl
        for pattern, cap in self.rules.get("level_caps", {}).items():
            if fnmatch(sid, pattern):
                level = min(level, cap)
        return level

    def _update_level(self, cid: str, sid: str, acc: dict, at: datetime) -> None:
        rows = self.store.skills[cid]
        current = next((r for r in rows if r["skill_id"] == sid and r["valid_to"] is None), None)
        new_level = self._level_for(sid, acc)
        if current is not None:
            current.update(evidence_points=round(acc["points"], 2), evidence_count=acc["count"],
                           max_trust_level=acc["max_trust"])
            if current["level"] == new_level:
                return
            current["valid_to"] = at.isoformat()
        old = current["level"] if current else 0
        rows.append({"skill_id": sid, "name": self.skill_names.get(sid, sid), "level": new_level,
                     "evidence_points": round(acc["points"], 2), "evidence_count": acc["count"],
                     "max_trust_level": acc["max_trust"], "valid_from": at.isoformat(), "valid_to": None})
        if new_level != old:
            self.store.outbox.append({"event_type": "SkillLevelChanged", "child_id": cid, "skill_id": sid,
                                      "old_level": old, "new_level": new_level})
        if new_level >= 3 and new_level > old:
            name = self.rules["levels"][new_level]["name"]
            self._achieve(cid, "skill_level", f"{self.skill_names.get(sid, sid)}: уровень {new_level}",
                          f"Уровень {new_level} ({name}) подтверждён наставником", acc.get("confirmed_issuer"), 2,
                          [sid], at, signer=acc.get("confirmed_by"))

    def _achieve(self, cid: str, atype: str, title: str, criteria: str, issuer: Optional[str], trust: int,
                 skills: list[str], at: datetime, signer: Optional[str] = None) -> dict:
        a = {"achievement_id": str(uuid.uuid4()), "type": atype, "title": title, "criteria": criteria,
             "issuer_id": issuer, "issuer_name": self.issuers.get(issuer or "", {}).get("name"),
             "signer_id": signer, "trust_level": trust, "skill_ids": skills, "issued_at": at.isoformat(),
             "revoked_at": None, "credential_id": None, "credential_jws": None, "exported": False}
        self.store.achievements[cid].append(a)
        self.store.outbox.append({"event_type": "AchievementIssued", "child_id": cid,
                                  "achievement_id": a["achievement_id"], "trust_level": trust})
        return a

    # ── Удобные выборки ────────────────────────────────────────

    def current_skills(self, cid: str) -> list[dict]:
        return sorted((r for r in self.store.skills.get(cid, []) if r["valid_to"] is None),
                      key=lambda r: (-r["level"], -r["evidence_points"]))

    def skill_level_at(self, cid: str, sid: str, when: datetime) -> int:
        """Уровень навыка на дату — то, ради чего нужна история SCD2."""
        for r in self.store.skills.get(cid, []):
            if r["skill_id"] != sid:
                continue
            start = datetime.fromisoformat(r["valid_from"])
            end = datetime.fromisoformat(r["valid_to"]) if r["valid_to"] else None
            if start <= when and (end is None or when < end):
                return r["level"]
        return 0
