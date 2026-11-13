"""Оценка объёма данных и нагрузки на паспорт.

Все допущения — параметры командной строки; значения по умолчанию взяты
из docs/assumptions.md (кейс + наши оценки). Пример:
    python load_estimate.py
    python load_estimate.py --concurrent 10000 --period 30     # худший случай
    python load_estimate.py --partner-camps 50                  # B2B-рост
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass

DAY = 86_400


@dataclass
class Params:
    kids_per_year: int = 10_000          # A1 [кейс]
    school_students: int = 300           # A2 [кейс]
    concurrent: int = 3_000              # A4 [оценка] детей в лагерях одновременно в пик
    period_s: int = 30                   # A3 [кейс] 30–60 с, считаем по худшему
    shift_days: int = 21                 # A5 [оценка]
    msg_bytes: int = 200                 # A6 [оценка]
    event_bytes: int = 1_024             # A7 [оценка]
    camp_events_per_kid_day: int = 10    # бронь, дневник, отзывы, проекты на смене [оценка]
    school_events_per_student_day: int = 20   # уроки, тесты [оценка]
    telemetry_ttl_days: int = 30
    clickhouse_compression: float = 8.0  # типичное сжатие телеметрии в ClickHouse [оценка]
    partner_camps: int = 0               # подключённые сторонние лагеря
    kids_per_partner_camp: int = 1_500   # детей за лето в одном партнёрском лагере [оценка]
    partner_events_per_kid: int = 5      # участие + 2–4 достижения за смену [оценка]
    peak_factor: float = 20.0            # пик к среднему (утренние отчёты, конец смены) [оценка]


def estimate(p: Params) -> dict:
    tel_per_kid_day = DAY // p.period_s
    tel_day = p.concurrent * tel_per_kid_day
    tel_raw_gb_day = tel_day * p.msg_bytes / 1e9
    tel_hot_gb = tel_raw_gb_day * p.telemetry_ttl_days / p.clickhouse_compression
    kid_days_year = p.kids_per_year * p.shift_days
    tel_year = kid_days_year * tel_per_kid_day

    camp_day = p.concurrent * p.camp_events_per_kid_day
    school_day = p.school_students * p.school_events_per_student_day
    activity_day = p.concurrent / 7                      # 1 агрегат на ребёнка в неделю
    partner_day = p.partner_camps * p.kids_per_partner_camp * p.partner_events_per_kid / 90   # за ~90 летних дней
    passport_day = camp_day + school_day + activity_day + partner_day
    passport_avg_rps = passport_day / DAY

    events_per_kid_year = (p.shift_days * p.camp_events_per_kid_day
                           + p.school_events_per_student_day * 150 * p.school_students / p.kids_per_year
                           + p.shift_days / 7)
    passport_year_gb = (p.kids_per_year * events_per_kid_year
                        + p.partner_camps * p.kids_per_partner_camp * p.partner_events_per_kid) * p.event_bytes / 1e9

    return {
        "Телеметрия: сообщений на ребёнка в сутки": tel_per_kid_day,
        "Телеметрия: сообщений в сутки (пик)": tel_day,
        "Телеметрия: сообщений в секунду (пик)": tel_day / DAY,
        "Телеметрия: ГБ в сутки до сжатия": tel_raw_gb_day,
        "Телеметрия: ГБ в ClickHouse за TTL (сжато)": tel_hot_gb,
        "Телеметрия: сообщений за год": tel_year,
        "Паспорт: событий лагеря в сутки": camp_day,
        "Паспорт: событий онлайн-школы в сутки": school_day,
        "Паспорт: агрегатов активности в сутки": activity_day,
        "Паспорт: событий партнёров в сутки": partner_day,
        "Паспорт: всего событий в сутки": passport_day,
        "Паспорт: событий в секунду (среднее)": passport_avg_rps,
        "Паспорт: событий в секунду (пик)": passport_avg_rps * p.peak_factor,
        "Паспорт: событий на ребёнка в год": events_per_kid_year,
        "Паспорт: журнал за год, ГБ": passport_year_gb,
        "Разница телеметрия / паспорт, раз": tel_day / passport_day,
    }


def fmt(v: float) -> str:
    if v >= 1e6:
        return f"{v / 1e6:,.1f} млн".replace(",", " ")
    if v >= 100:
        return f"{v:,.0f}".replace(",", " ")
    return f"{v:,.2f}".replace(",", " ")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name, default in Params().__dict__.items():
        flag = "--period" if name == "period_s" else "--" + name.replace("_", "-")
        ap.add_argument(flag, dest=name, type=type(default), default=default)
    p = Params(**vars(ap.parse_args()))
    print("| Показатель | Значение |\n|---|---|")
    for k, v in estimate(p).items():
        print(f"| {k} | {fmt(v)} |")


if __name__ == "__main__":
    main()
