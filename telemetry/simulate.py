"""Синтетическая телеметрия браслета (как ChildStatusUpdated): для генератора и тестов."""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone
from typing import Iterator


def simulate(bracelet_id: str, start: date, days: int, active_minutes: int, sleep_hour: float,
             sleep_jitter_min: float, seed: int = 0, period_s: int = 60) -> Iterator[dict]:
    """active_minutes — средняя активность в день; sleep_hour — среднее время засыпания (например 22.0)."""
    rnd = random.Random(seed)
    for d in range(days):
        day = start + timedelta(days=d)
        act_today = max(0, int(rnd.gauss(active_minutes, active_minutes * 0.25)))
        act_start = 10 * 60                                     # активность блоком с 10:00
        onset = sleep_hour * 60 + rnd.gauss(0, sleep_jitter_min)
        for i in range(24 * 3600 // period_s):
            minute = i * period_s / 60
            ts = datetime.combine(day, datetime.min.time(), timezone.utc) + timedelta(minutes=minute)
            active = act_start <= minute < act_start + act_today
            asleep = minute >= onset or minute < 7 * 60
            yield {"bracelet_id": bracelet_id, "ts": ts,
                   "accel_g": 1.8 if active else (0.2 if asleep else 1.0),
                   "hr": 140 if active else 70, "lat": 55.69, "lon": 37.35,   # агрегатор их не читает
                   "asleep": asleep}
