"""Матрица ролей и доступа паспорта + «право ребёнка скрыть».

Одна функция решает всё: `decide(viewer, ctx, category, action, item)`.
Решение складывается из четырёх проверок по порядку:
  1. матрица: есть ли у роли вообще право на категорию данных;
  2. область: «свои» дети (по участию в смене/курсе) или согласие на передачу партнёру;
  3. согласие на цель: активность и рекомендации видны только при действующем согласии;
  4. видимость: что ребёнок или родитель скрыли (VisibilityRule) с учётом возраста.

В продакшене та же логика живёт в Passport API (Go) поверх ролей Keycloak (ABAC);
здесь — эталонная реализация, по которой пишутся тесты.

Здоровье и геопозиция в паспорт не входят, поэтому в матрице их нет:
к ним применяются правила модуля лагеря и контура телеметрии (docs/access_matrix.md).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal, Optional

Role = Literal["child", "guardian", "counselor", "teacher", "camp_admin", "partner"]
Category = Literal["profile", "skills", "achievements", "observations", "projects",
                   "activity", "recommendations", "consents"]
Action = Literal["read", "write"]
AgeBand = Literal["4-9", "10-13", "14-17", "18+"]

ROLES: tuple[Role, ...] = ("child", "guardian", "counselor", "teacher", "camp_admin", "partner")
CATEGORIES: tuple[Category, ...] = ("profile", "skills", "achievements", "observations", "projects",
                                    "activity", "recommendations", "consents")

# ── 1. Матрица ────────────────────────────────────────────────
# Значение — строка прав: R (читать), W (писать). Суффикс задаёт область:
#   ""         — без ограничения области (ребёнок и родитель видят свой паспорт);
#   ":own"     — только дети своих смен/курсов, пока идёт участие и 30 дней после;
#   ":consent" — только по согласию share_partner на эту категорию;
#   ":10+"     — только с 10 лет (наблюдения наставника ребёнку младше 10 показывает родитель).
# Отсутствие ключа = нет доступа.
MATRIX: dict[Category, dict[Role, str]] = {
    "profile":         {"child": "R",  "guardian": "RW", "counselor": "R:own",  "teacher": "R:own",
                        "camp_admin": "R:own", "partner": "R:consent"},
    "skills":          {"child": "R",  "guardian": "R",  "counselor": "R:own",  "teacher": "R:own",
                        "camp_admin": "R:own", "partner": "R:consent"},
    "achievements":    {"child": "R",  "guardian": "R",  "counselor": "R:own",  "teacher": "R:own",
                        "camp_admin": "R:own", "partner": "R:consent"},
    "observations":    {"child": "R:10+", "guardian": "R", "counselor": "RW:own", "teacher": "RW:own",
                        "camp_admin": "R:own"},
    "projects":        {"child": "RW", "guardian": "R",  "counselor": "RW:own", "teacher": "RW:own",
                        "camp_admin": "R:own", "partner": "R:consent"},
    "activity":        {"child": "R",  "guardian": "R",  "counselor": "R:own"},
    "recommendations": {"child": "R",  "guardian": "R",  "teacher": "R:own"},
    "consents":        {"child": "R",  "guardian": "RW", "camp_admin": "R:own", "partner": "R:consent"},
}

# ── 3. Категории, которые существуют только при согласии на свою цель ──
PURPOSE_OF_CATEGORY: dict[Category, str] = {
    "activity": "activity",
    "recommendations": "recommendations",
}

# ── 4. От кого и в каком возрасте ребёнок может скрыть элемент ──
# Политика продукта, не требование закона: согласовать с юристом (docs/assumptions.md, L9).
CHILD_CAN_HIDE_FROM: dict[AgeBand, frozenset[str]] = {
    "4-9":   frozenset(),                                                    # видимостью управляет родитель
    "10-13": frozenset({"partner", "export", "teacher", "counselor"}),
    "14-17": frozenset({"partner", "export", "teacher", "counselor", "guardian"}),
    "18+":   frozenset({"partner", "export", "teacher", "counselor", "guardian", "camp_admin"}),
}
GUARDIAN_CAN_HIDE_FROM = frozenset({"partner", "export", "teacher", "counselor"})

OWN_SCOPE_GRACE = timedelta(days=30)   # сколько вожатый видит ребёнка после конца смены
OWN_SCOPE_LEAD = timedelta(days=7)     # и сколько до начала (подготовка отрядов)


@dataclass(frozen=True)
class Viewer:
    role: Role
    sub: str                                    # keycloak_sub
    child_id: Optional[str] = None              # для роли child
    guardian_of: frozenset[str] = frozenset()   # для роли guardian
    program_ids: frozenset[str] = frozenset()   # вожатый/преподаватель: назначенные смены и курсы
    issuer_id: Optional[str] = None             # админ лагеря и партнёр: свой лагерь


@dataclass(frozen=True)
class Participation:
    program_id: str
    issuer_id: str
    started_at: date
    ended_at: Optional[date]


@dataclass(frozen=True)
class Consent:
    purpose: str
    data_categories: frozenset[str] = frozenset()
    partner_issuer_id: Optional[str] = None


@dataclass(frozen=True)
class VisibilityRule:
    item_type: str
    item_id: str
    hidden_from: frozenset[str]
    set_by: Literal["child", "guardian"]


@dataclass
class ChildContext:
    """Всё о ребёнке, что нужно для решения о доступе."""
    child_id: str
    age_band: AgeBand
    participations: list[Participation] = field(default_factory=list)
    consents: list[Consent] = field(default_factory=list)          # только действующие
    visibility: list[VisibilityRule] = field(default_factory=list)

    def has_consent(self, purpose: str, partner_issuer_id: Optional[str] = None,
                    category: Optional[str] = None) -> bool:
        for c in self.consents:
            if c.purpose != purpose:
                continue
            if partner_issuer_id is not None and c.partner_issuer_id != partner_issuer_id:
                continue
            if category is not None and category not in c.data_categories:
                continue
            return True
        return False


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    placeholder: bool = False   # True: показать «скрыто ребёнком» вместо содержимого


ITEM_TYPE_OF_CATEGORY = {"achievements": "achievement", "observations": "observation",
                         "projects": "project", "skills": "skill", "activity": "activity"}


def _in_own_scope(viewer: Viewer, ctx: ChildContext, today: date) -> bool:
    for p in ctx.participations:
        if viewer.role == "camp_admin":
            if p.issuer_id == viewer.issuer_id:
                return True
            continue
        if p.program_id not in viewer.program_ids:
            continue
        end = p.ended_at or today
        if p.started_at - OWN_SCOPE_LEAD <= today <= end + OWN_SCOPE_GRACE:
            return True
    return False


def decide(viewer: Viewer, ctx: ChildContext, category: Category, action: Action = "read",
           item_id: Optional[str] = None, item_author_sub: Optional[str] = None,
           today: Optional[date] = None) -> Decision:
    today = today or date.today()

    # 0. Собственный паспорт и законный представитель
    if viewer.role == "child" and viewer.child_id != ctx.child_id:
        return Decision(False, "чужой паспорт")
    if viewer.role == "guardian" and ctx.child_id not in viewer.guardian_of:
        return Decision(False, "не законный представитель этого ребёнка")

    # 1. Матрица
    rule = MATRIX[category].get(viewer.role)
    if rule is None:
        return Decision(False, f"роль {viewer.role} не имеет доступа к {category}")
    rights, _, scope = rule.partition(":")
    if (action == "read" and "R" not in rights) or (action == "write" and "W" not in rights):
        return Decision(False, f"роль {viewer.role}: нет права {action} на {category}")

    # 2. Область
    if scope == "own" and not _in_own_scope(viewer, ctx, today):
        return Decision(False, "ребёнок не из вашей смены или курса")
    if scope == "consent" and not ctx.has_consent(
            "share_partner", viewer.issuer_id, None if category == "consents" else category):
        return Decision(False, "нет согласия родителя на передачу этой категории вашему лагерю")
    if scope == "10+" and ctx.age_band == "4-9":
        return Decision(False, "наблюдения наставника ребёнку до 10 лет показывает родитель")

    # 3. Согласие на цель
    purpose = PURPOSE_OF_CATEGORY.get(category)
    if purpose and not ctx.has_consent(purpose):
        return Decision(False, f"нет согласия на цель «{purpose}»")

    # 4. Видимость конкретного элемента
    if item_id is not None and viewer.role not in ("child",):
        item_type = ITEM_TYPE_OF_CATEGORY.get(category)
        for v in ctx.visibility:
            if v.item_type == item_type and v.item_id == item_id and viewer.role in v.hidden_from:
                if item_author_sub is not None and item_author_sub == viewer.sub:
                    break                     # автор наблюдения всегда видит своё
                if viewer.role == "guardian":
                    return Decision(False, "скрыто ребёнком", placeholder=True)
                return Decision(False, "элемент скрыт")

    return Decision(True, "ok")


def can_set_visibility(age_band: AgeBand, set_by: Literal["child", "guardian"],
                       hidden_from: frozenset[str]) -> tuple[bool, str]:
    """Проверяет, может ли ребёнок (с учётом возраста) или родитель скрыть элемент от этих ролей."""
    allowed = CHILD_CAN_HIDE_FROM[age_band] if set_by == "child" else GUARDIAN_CAN_HIDE_FROM
    extra = hidden_from - allowed
    if extra:
        who = f"ребёнок {age_band} лет" if set_by == "child" else "родитель"
        return False, f"{who} не может скрыть от: {', '.join(sorted(extra))}"
    return True, "ok"


def matrix_markdown() -> str:
    """Матрица в виде таблицы для отчёта (генерируется из кода, чтобы не расходилась)."""
    names = {"child": "Ребёнок", "guardian": "Родитель", "counselor": "Вожатый",
             "teacher": "Преподаватель", "camp_admin": "Админ лагеря", "partner": "Лагерь-партнёр"}
    cats = {"profile": "Профиль", "skills": "Навыки", "achievements": "Достижения",
            "observations": "Наблюдения наставника", "projects": "Паспорта проектов",
            "activity": "Активность (недельные уровни)", "recommendations": "Рекомендации",
            "consents": "Согласия"}
    scope_txt = {"": "", "own": " свои", "consent": " С", "10+": " с 10 лет"}
    rights_txt = {"R": "Ч", "RW": "Ч, З"}
    lines = ["| Данные | " + " | ".join(names[r] for r in ROLES) + " |",
             "|---" * (len(ROLES) + 1) + "|"]
    for c in CATEGORIES:
        cells = []
        for r in ROLES:
            rule = MATRIX[c].get(r)
            if rule is None:
                cells.append("—")
                continue
            rights, _, scope = rule.partition(":")
            cell = rights_txt[rights] + scope_txt[scope]
            if c in PURPOSE_OF_CATEGORY:
                cell += " (согл.)"
            cells.append(cell)
        lines.append(f"| {cats[c]} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    print(matrix_markdown())
