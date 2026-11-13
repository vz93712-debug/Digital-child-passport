"""Хранилище паспорта в памяти: заменяет PostgreSQL в прототипе.

Таблицы повторяют schema/ddl.sql по смыслу. Журнал событий хранит payload
только в зашифрованном виде (ключ ребёнка из privacy.keys.KeyStore).
"""
from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

# Таблицы-проекции, которые строятся из журнала и стираются при удалении.
PROJECTIONS = ("participations", "skills", "achievements", "observations", "projects",
               "activity", "recommendations", "visibility")


@dataclass
class Store:
    children: dict[str, dict[str, Any]] = field(default_factory=dict)
    external_ids: dict[tuple[str, str], str] = field(default_factory=dict)   # (source, ext_id) → child_id
    events: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    seen_event_ids: set[str] = field(default_factory=set)
    consents: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    # проекции: child_id → список записей
    participations: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    skills: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    achievements: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    observations: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    projects: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    activity: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    recommendations: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    visibility: dict[str, list] = field(default_factory=lambda: defaultdict(list))
    # аналитика (ClickHouse): строки по псевдониму, без child_id
    analytics: list[dict[str, Any]] = field(default_factory=list)
    # каким партнёрам что передавали (для уведомления при удалении)
    partner_shares: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))
    # отозванные бейджи (Bitstring Status List): credential_id → причина
    revoked_credentials: dict[str, str] = field(default_factory=dict)
    # исходящие уведомления (webhooks партнёрам, события в шину)
    outbox: list[dict[str, Any]] = field(default_factory=list)
    # «Сколько памяти держит ребёнок» — для проверок в тестах
    def footprint(self, child_id: str) -> dict[str, int]:
        return {t: len(getattr(self, t).get(child_id, [])) for t in PROJECTIONS} | {
            "events": len(self.events.get(child_id, [])),
            "consents": len(self.consents.get(child_id, [])),
        }

    def snapshot(self) -> "Store":
        """Имитация бэкапа: полная копия на момент вызова."""
        return copy.deepcopy(self)
