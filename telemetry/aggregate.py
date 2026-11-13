"""Агрегатор телеметрии браслетов: сырые сообщения → недельные уровни активности.

Это граница «паспорт — не инструмент слежки». На вход приходят сообщения браслета
(ChildStatusUpdated: время, ускорение, пульс, координаты, признак сна). На выходе —
одно событие ActivityWeeklySummarized на ребёнка в неделю с тремя дискретными уровнями.
Пульс и координаты агрегатор не читает вообще, минутных рядов в паспорте нет.

Пороги:
  * активная минута — ускорение выше 1.3 g (умеренная и высокая активность);
  * activity_level по среднему числу активных минут в день: < 60 low, 60–119 medium, ≥ 120 high
    (60 минут в день — рекомендация ВОЗ по физической активности для детей 5–17 лет);
  * regularity по доле дней с ≥ 60 активными минутами: < 50% irregular, < 85% regular, иначе very_regular;
  * sleep_regularity по разбросу времени засыпания: стандартное отклонение < 45 минут — regular.
Неделя без минимум 3 дней данных не агрегируется (слишком мало, чтобы делать выводы о ребёнке).

В продакшене это materialized view в ClickHouse плюс публикация события раз в неделю;
событие проходит consent gate паспорта (цель «activity»), как любое другое.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

from passport.events import Envelope

ACTIVE_G = 1.3
WHO_MINUTES = 60
MIN_DAYS = 3


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _level(avg_minutes: float) -> str:
    return "low" if avg_minutes < WHO_MINUTES else "medium" if avg_minutes < 2 * WHO_MINUTES else "high"


def _regularity(share: float) -> str:
    return "irregular" if share < 0.5 else "regular" if share < 0.85 else "very_regular"


def _sleep(onsets_min: list[float]) -> Optional[str]:
    if len(onsets_min) < MIN_DAYS:
        return None
    return "regular" if statistics.pstdev(onsets_min) < 45 else "irregular"


def aggregate(samples: Iterable[dict], period_s: int = 60) -> list[Envelope]:
    """samples: {'bracelet_id', 'ts' (datetime UTC), 'accel_g', 'asleep', ...}; лишние поля игнорируются."""
    active = defaultdict(lambda: defaultdict(float))     # (bracelet, week) → day → активные минуты
    days_seen = defaultdict(set)
    onsets = defaultdict(dict)                           # (bracelet, week) → ночь → минута засыпания
    for s in samples:
        ts: datetime = s["ts"]
        key = (s["bracelet_id"], week_start(ts.date()))
        days_seen[key].add(ts.date())
        if s["accel_g"] > ACTIVE_G:
            active[key][ts.date()] += period_s / 60
        if s["asleep"]:
            # засыпание — первый «сон» после 18:00; ночь относим к дате вечера
            night = ts.date() if ts.hour >= 18 else ts.date() - timedelta(days=1)
            minute = (ts.hour * 60 + ts.minute) if ts.hour >= 18 else (24 * 60 + ts.hour * 60 + ts.minute)
            if ts.hour >= 18 or ts.hour < 6:
                onsets[key].setdefault(night, minute)
    out = []
    for key, days in sorted(days_seen.items()):
        if len(days) < MIN_DAYS:
            continue
        bracelet, wk = key
        per_day = [active[key].get(d, 0.0) for d in days]
        payload = {
            "week_start": wk.isoformat(),
            "activity_level": _level(statistics.mean(per_day)),
            "regularity": _regularity(sum(m >= WHO_MINUTES for m in per_day) / len(per_day)),
            "sleep_regularity": _sleep(list(onsets[key].values())),
        }
        out.append(Envelope(event_type="ActivityWeeklySummarized", source="telemetry", child_ref=bracelet,
                            occurred_at=datetime.combine(wk + timedelta(days=7), datetime.min.time(), timezone.utc),
                            payload=payload))
    return out
