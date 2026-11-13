"""Каталог событий паспорта: конверт и схемы payload.

Все источники (ядро лагеря, онлайн-школа через outbox, агрегатор телеметрии, Partner API)
присылают события в одном конверте. Схемы payload версионируются полем schema_version.
Здесь же — таблица «какое событие требует какого согласия», которую использует consent gate.
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Optional
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Envelope(BaseModel):
    """Общий конверт события (одинаковый в RabbitMQ, AsyncAPI и прототипе)."""

    event_id: UUID = Field(default_factory=uuid4, description="Уникален глобально, по нему дедупликация")
    event_type: str
    schema_version: int = 1
    source: str = Field(description="camp_core | online_school | projects | telemetry | partner:<issuer_id> | passport")
    child_ref: str = Field(description="ID ребёнка в системе-источнике; паспорт сам переводит его в child_id")
    occurred_at: datetime
    payload: dict


# ── Ядро лагеря ────────────────────────────────────────────────

class BookingConfirmed(BaseModel):
    booking_id: str
    program_id: str
    starts_on: date
    ends_on: date


class ShiftCompleted(BaseModel):
    booking_id: str
    program_id: str
    attended_days: int
    total_days: int


class MentorReviewAdded(BaseModel):
    review_id: str
    program_id: str
    author_sub: str = Field(description="keycloak_sub вожатого или преподавателя")
    text: str
    skill_ids: list[str]
    confirms_level: Optional[int] = Field(None, ge=3, le=4, description="Наставник подтверждает уровень 3 или 4")
    signature: str


# ── Онлайн-школа (через transactional outbox) ─────────────────

class LessonCompleted(BaseModel):
    lesson_id: str
    course_id: str
    duration_min: int


class TestPassed(BaseModel):
    test_id: str
    course_id: str
    score_pct: float = Field(ge=0, le=100)


class CourseCompleted(BaseModel):
    course_id: str
    final_score_pct: Optional[float] = Field(None, ge=0, le=100)


# ── Проекты ────────────────────────────────────────────────────

class ProjectSubmitted(BaseModel):
    project_id: str
    program_id: str
    title: str
    goal: str
    result_url: Optional[str] = None
    role: str
    team_child_refs: list[str] = []


class ProjectReviewed(BaseModel):
    project_id: str
    program_id: str
    mentor_sub: str
    accepted: bool
    skill_ids: list[str]
    confirms_level: Optional[int] = Field(None, ge=3, le=4)
    signature: str


# ── Агрегатор телеметрии (из ChildStatusUpdated) ───────────────

class ActivityWeeklySummarized(BaseModel):
    week_start: date
    activity_level: Literal["low", "medium", "high"]
    regularity: Literal["irregular", "regular", "very_regular"]
    sleep_regularity: Optional[Literal["irregular", "regular"]] = None


# ── Partner API ────────────────────────────────────────────────

class PartnerParticipationReported(BaseModel):
    program_id: str
    starts_on: date
    ends_on: Optional[date] = None
    status: Literal["planned", "active", "completed", "cancelled"]


class PartnerAchievementSubmitted(BaseModel):
    title: str
    criteria: str
    skill_ids: list[str]
    program_id: str
    credential_jws: Optional[str] = Field(None, description="Подписанный Open Badge 3.0 → L3; без подписи → L1")


# ── Сервис согласий и приложения ──────────────────────────────

Purpose = Literal["passport", "activity", "recommendations", "share_partner", "export"]


class ConsentGranted(BaseModel):
    consent_id: str
    guardian_sub: str
    purpose: Purpose
    partner_issuer_id: Optional[str] = None
    text_version: str


class ConsentRevoked(BaseModel):
    consent_id: str
    purpose: Purpose


class VisibilityChanged(BaseModel):
    item_type: Literal["achievement", "observation", "project", "skill", "activity"]
    item_id: str
    hidden_from: list[str]
    set_by: Literal["child", "guardian"]


class DeletionRequested(BaseModel):
    request_id: str
    guardian_sub: str


PAYLOADS: dict[str, type[BaseModel]] = {
    cls.__name__: cls
    for cls in [
        BookingConfirmed, ShiftCompleted, MentorReviewAdded,
        LessonCompleted, TestPassed, CourseCompleted,
        ProjectSubmitted, ProjectReviewed, ActivityWeeklySummarized,
        PartnerParticipationReported, PartnerAchievementSubmitted,
        ConsentGranted, ConsentRevoked, VisibilityChanged, DeletionRequested,
    ]
}

# Consent gate: какое согласие должно действовать, чтобы событие сохранилось.
# None — событие служебное и принимается всегда (сами согласия, удаление, видимость).
REQUIRED_CONSENT: dict[str, Optional[str]] = {
    "BookingConfirmed": "passport",
    "ShiftCompleted": "passport",
    "MentorReviewAdded": "passport",
    "LessonCompleted": "passport",
    "TestPassed": "passport",
    "CourseCompleted": "passport",
    "ProjectSubmitted": "passport",
    "ProjectReviewed": "passport",
    "ActivityWeeklySummarized": "activity",
    "PartnerParticipationReported": "share_partner",
    "PartnerAchievementSubmitted": "share_partner",
    "ConsentGranted": None,
    "ConsentRevoked": None,
    "VisibilityChanged": None,
    "DeletionRequested": None,
}

# Исходящие события паспорта (публикуются в platform.events).
OUTGOING = ["SkillLevelChanged", "AchievementIssued", "RecommendationsUpdated"]


def validate(envelope: Envelope) -> BaseModel:
    """Проверяет payload по схеме своего типа. Неизвестный тип или битый payload → ValueError (уходит в DLQ)."""
    model = PAYLOADS.get(envelope.event_type)
    if model is None:
        raise ValueError(f"unknown event_type {envelope.event_type!r}")
    return model.model_validate(envelope.payload)
