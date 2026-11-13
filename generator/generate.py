"""Генератор синтетического мира: дети, согласия и год событий из трёх модулей и от партнёров.

Учебный год 2026/27 + лето 2027. Детерминирован (seed). Состав:
  * N детей (по умолчанию 200) с разными ID в лагере, онлайн-школе, браслете и у партнёра;
  * согласия: паспорт ~95%, рекомендации ~80%, активность ~60%, экспорт ~30%;
  * онлайн-школа: уроки, тесты, завершение курсов; лагерь: брони, смены, отзывы наставников;
  * проекты с защитой; партнёрские смены; телеметрия браслетов → недельные агрегаты;
  * граничные случаи: дубликат, неизвестный ребёнок, битый payload, событие без согласия,
    достижение партнёра без участия в смене.
  * героиня демо — Маша, 12 лет, со сценарием, который проходит через все механизмы паспорта.

Запуск:  python -m generator.generate            # сводка по прогону
         python -m generator.generate --jsonl out.jsonl   # выгрузить события
"""
from __future__ import annotations

import argparse
import json
import random
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Optional

from passport.events import Envelope
from passport.service import PassportService, load_catalog
from passport.store import Store
from privacy.consent import ConsentService
from privacy.keys import KeyStore
from telemetry.aggregate import aggregate
from telemetry.simulate import simulate

YEAR_START = date(2026, 9, 1)
REGISTERED_AT = datetime(2026, 9, 1, tzinfo=timezone.utc)
TELEMETRY_PERIOD_S = 300          # в генераторе реже, чем у браслета (60 с), чтобы прогон шёл секунды
NAMES = ["Маша", "Петя", "Аня", "Ваня", "Соня", "Миша", "Лиза", "Артём", "Даша", "Егор", "Полина", "Тимур",
         "Алиса", "Гоша", "Вера", "Лёва", "Катя", "Саша", "Ника", "Федя"]

ISSUERS, PROGRAMS = load_catalog()
P1 = "00000000-0000-4000-8000-000000000101"   # партнёр с ключом (tier 3)
P2 = "00000000-0000-4000-8000-000000000102"   # партнёр без ключа (tier 1)


@dataclass
class ChildSpec:
    name: str
    birth_year: int
    interests: list[str]
    external_ids: list[tuple[str, str]]
    guardian_sub: str
    consents: list[dict]            # {"purpose", "data_categories"?, "partner_issuer_id"?}
    hero: bool = False


@dataclass
class World:
    children: list[ChildSpec]
    events: list[Envelope]
    telemetry_samples: int = 0
    edge_cases: dict[str, str] = field(default_factory=dict)    # event_id → что проверяет


def at(d: date, hour: int = 12) -> datetime:
    return datetime.combine(d, time(hour), timezone.utc)


_IDS = random.Random(0)     # детерминированные id событий и объектов; сбрасывается в build()


def new_id() -> uuid.UUID:
    return uuid.UUID(int=_IDS.getrandbits(128), version=4)


def ev(etype: str, source: str, ref: str, when: datetime, **payload) -> Envelope:
    return Envelope(event_id=new_id(), event_type=etype, source=source, child_ref=ref, occurred_at=when,
                    payload=payload)


def eligible(age: int, kinds: set[str], issuers: Optional[set[str]] = None) -> list[dict]:
    return [p for p in PROGRAMS.values() if p["kind"] in kinds and p["age_min"] <= age <= p["age_max"]
            and (issuers is None or p["issuer_id"] in issuers)]


def pick(rnd: random.Random, programs: list[dict], interests: list[str], k: int) -> list[dict]:
    """Выбор программ с весом по совпадению с интересами — дети чаще идут туда, что им интересно."""
    pool, out = list(programs), []
    while pool and len(out) < k:
        weights = [1 + 3 * len(set(p["skill_tags"]) & set(interests)) for p in pool]
        choice = rnd.choices(pool, weights)[0]
        pool.remove(choice)
        out.append(choice)
    return out


def course_events(rnd, ref, prog, start: date, lessons: int, score: float, complete: bool) -> list[Envelope]:
    out, d = [], start
    for i in range(lessons):
        out.append(ev("LessonCompleted", "online_school", ref, at(d, 17), lesson_id=f"{prog['program_id']}.l{i + 1}",
                      course_id=prog["program_id"], duration_min=45))
        d += timedelta(days=rnd.choice([3, 4, 7]))
    out.append(ev("TestPassed", "online_school", ref, at(d, 18), test_id=f"{prog['program_id']}.final",
                  course_id=prog["program_id"], score_pct=round(score, 1)))
    if complete:
        out.append(ev("CourseCompleted", "online_school", ref, at(d + timedelta(days=1), 18),
                      course_id=prog["program_id"], final_score_pct=round(score, 1)))
    return out


def shift_events(rnd, ref, prog, attended: int, review: Optional[dict]) -> list[Envelope]:
    s, e = date.fromisoformat(prog["starts_on"]), date.fromisoformat(prog["ends_on"])
    total = (e - s).days + 1
    out = [ev("BookingConfirmed", "camp_core", ref, at(s - timedelta(days=rnd.randint(30, 90)), 10),
              booking_id=f"bk-{new_id().hex[:8]}", program_id=prog["program_id"],
              starts_on=s.isoformat(), ends_on=e.isoformat())]
    if review:
        out.append(ev("MentorReviewAdded", "camp_core", ref, at(e - timedelta(days=1), 20),
                      review_id=f"rv-{new_id().hex[:8]}", program_id=prog["program_id"], **review))
    out.append(ev("ShiftCompleted", "camp_core", ref, at(e, 15), booking_id=f"bk-{ref}-{prog['program_id']}",
                  program_id=prog["program_id"], attended_days=min(attended, total), total_days=total))
    return out


def telemetry_events(bracelet: str, prog: dict, active_min: int, sleep_hour: float, jitter: float, seed: int):
    s, e = date.fromisoformat(prog["starts_on"]), date.fromisoformat(prog["ends_on"])
    samples = list(simulate(bracelet, s, (e - s).days + 1, active_min, sleep_hour, jitter, seed, TELEMETRY_PERIOD_S))
    return aggregate(samples, TELEMETRY_PERIOD_S), len(samples)


def project_title(catalog_title: str) -> str:
    """«Проект «Умный лагерь» (IoT)» → «Умный лагерь (IoT)»: в паспорте название без слова «Проект»."""
    if "«" not in catalog_title:
        return catalog_title
    inner, tail = catalog_title.split("«", 1)[1].split("»", 1)
    return inner + tail


def project_events(rnd, ref, prog, start: date, accepted: bool, confirm: Optional[int], role: str) -> list[Envelope]:
    pid = f"pr-{new_id().hex[:8]}"
    out = [ev("ProjectSubmitted", "projects", ref, at(start, 16), project_id=pid, program_id=prog["program_id"],
              title=project_title(prog["title"]), goal="Сделать работающий прототип за 4 недели",
              result_url=f"https://projects.sportzania.example/{pid}", role=role)]
    review = dict(project_id=pid, program_id=prog["program_id"], mentor_sub=f"kc-mentor-{rnd.randint(1, 9)}",
                  accepted=accepted, skill_ids=prog["skill_tags"], signature="sig-placeholder")
    if confirm:
        review["confirms_level"] = confirm
    out.append(ev("ProjectReviewed", "projects", ref, at(start + timedelta(days=7), 16), **review))
    return out


def hero(sign: Optional[Callable[[dict], str]]) -> tuple[ChildSpec, list[Envelope], int]:
    """Маша, 12 лет: Python онлайн → смена «ИИ и роботы» → проект «Умный лагерь» → смена у партнёра."""
    rnd = random.Random(7)
    refs = {"camp_core": "camp-masha", "online_school": "sch-masha", "telemetry": "br-masha",
            "projects": "pr-masha", f"partner:{P1}": "sosny-0153"}
    spec = ChildSpec("Маша", 2014, ["tech.robotics", "tech.python"], list(refs.items()), "kc-parent-masha",
                     [{"purpose": "passport"}, {"purpose": "recommendations"}, {"purpose": "activity"},
                      {"purpose": "export"},
                      {"purpose": "share_partner", "partner_issuer_id": P1, "data_categories": ("achievements",)}],
                     hero=True)
    evs = course_events(rnd, refs["online_school"], PROGRAMS["course.python-start"], date(2026, 10, 5), 12, 85, True)
    robots = PROGRAMS["shift.robots-ai"]
    evs += shift_events(rnd, refs["camp_core"], robots, 21, {
        "author_sub": "kc-counselor-olga", "text": "Маша собрала манипулятор и помогала отряду с кодом.",
        "skill_ids": ["tech.robotics", "social.teamwork"], "signature": "sig-placeholder"})
    evs += project_events(rnd, refs["projects"], PROGRAMS["project.smart-camp"], date(2027, 6, 12), True, 3,
                          "программист")
    tel, n = telemetry_events(refs["telemetry"], robots, 130, 22.0, 15, seed=7)
    evs += tel
    drones = PROGRAMS["partner.sosny.drones"]
    evs.append(ev("PartnerParticipationReported", f"partner:{P1}", refs[f"partner:{P1}"], at(date(2027, 7, 21), 12),
                  program_id=drones["program_id"], starts_on=drones["starts_on"], ends_on=drones["ends_on"],
                  status="completed"))
    award = {"title": "Пилот беспилотника — базовый уровень", "criteria": "5 учебных полётов и зачёт инструктору",
             "skill_ids": ["tech.drones"], "program_id": drones["program_id"]}
    when = at(date(2027, 7, 21), 13)
    evs.append(ev("PartnerAchievementSubmitted", f"partner:{P1}", refs[f"partner:{P1}"], when,
                  credential_jws=sign(award, refs[f"partner:{P1}"], when) if sign else None, **award))
    return spec, evs, n


Signer = Callable[[dict, str, datetime], str]      # (award, child_ref у партнёра, когда) → VC-JWT


def partner_signer(key) -> Signer:
    """Партнёр подписывает свои бейджи своим ключом (в демо подделка — тот же партнёр, но чужой ключ)."""
    from verify.badges import build_credential, sign
    issuer = ISSUERS[key.issuer_id]

    def _sign(award: dict, holder_ref: str, when: datetime) -> str:
        cred = build_credential(issuer, award["title"], award["criteria"], "award", award["skill_ids"],
                                holder_ref, when, credential_id=f"urn:uuid:{new_id()}")
        return sign(cred, key)
    return _sign


def build(n: int = 200, seed: int = 42, sign: Optional[Signer] = None, forge: Optional[Signer] = None) -> World:
    rnd = random.Random(seed)
    _IDS.seed(seed + 1)
    spec, events, samples = hero(sign)
    children = [spec]
    for i in range(1, n):
        by = rnd.randint(2010, 2020)
        age = 2027 - by
        interests = rnd.sample(sorted({t for p in PROGRAMS.values() for t in p["skill_tags"]}), rnd.randint(2, 3))
        refs: dict[str, str] = {}
        in_camp, in_school = rnd.random() < 0.75, rnd.random() < 0.4
        if in_camp:
            refs["camp_core"], refs["telemetry"] = f"camp-{i:05d}", f"br-{i:05d}"
        if in_school:
            refs["online_school"] = f"sch-{i:04d}"
        refs["projects"] = f"pr-{i:05d}"
        consents = [{"purpose": "passport"}] if rnd.random() < 0.95 else []
        consents += [{"purpose": p} for p, prob in (("recommendations", .8), ("activity", .6), ("export", .3))
                     if rnd.random() < prob]
        partner = rnd.random() < 0.08 and age >= 10
        if partner:
            refs[f"partner:{P2}"] = f"volna-{i:04d}"
            if rnd.random() < 0.7:
                consents.append({"purpose": "share_partner", "partner_issuer_id": P2,
                                 "data_categories": ("achievements",)})
        children.append(ChildSpec(rnd.choice(NAMES), by, interests, list(refs.items()), f"kc-parent-{i:05d}", consents))

        if in_school:
            for k, prog in enumerate(pick(rnd, eligible(age, {"course"}), interests, rnd.randint(1, 3))):
                start = YEAR_START + timedelta(days=rnd.randint(10, 200))
                events += course_events(rnd, refs["online_school"], prog, start, rnd.randint(6, 16),
                                        rnd.uniform(45, 100), rnd.random() < 0.75)
        if in_camp:
            shifts = pick(rnd, eligible(age, {"shift"}, {"00000000-0000-4000-8000-000000000001"}), interests,
                          rnd.choice([1, 1, 2]))
            for prog in shifts:
                review = None
                if rnd.random() < 0.5:
                    review = {"author_sub": f"kc-counselor-{rnd.randint(1, 30)}", "text": "Активно участвовал(а) в жизни отряда.",
                              "skill_ids": rnd.sample(prog["skill_tags"], 1), "signature": "sig-placeholder"}
                    if rnd.random() < 0.2:
                        review["confirms_level"] = 3
                events += shift_events(rnd, refs["camp_core"], prog, rnd.randint(14, 21), review)
                tel, cnt = telemetry_events(refs["telemetry"], prog, rnd.choice([30, 70, 100, 140]),
                                            rnd.uniform(21, 23), rnd.choice([10, 30, 80]), seed=i)
                events += tel
                samples += cnt
        if rnd.random() < 0.25:
            projs = eligible(age, {"project_track"})
            if projs:
                prog = pick(rnd, projs, interests, 1)[0]
                events += project_events(rnd, refs["projects"], prog, YEAR_START + timedelta(days=rnd.randint(30, 250)),
                                         rnd.random() < 0.85, 3 if rnd.random() < 0.3 else None,
                                         rnd.choice(["капитан", "программист", "дизайнер", "исследователь"]))
        if partner:
            prog = PROGRAMS["partner.volna.swim"]
            if prog["age_min"] <= age <= prog["age_max"]:
                ref = refs[f"partner:{P2}"]
                events.append(ev("PartnerParticipationReported", f"partner:{P2}", ref, at(date(2027, 6, 30)),
                                 program_id=prog["program_id"], starts_on=prog["starts_on"], ends_on=prog["ends_on"],
                                 status="completed"))
                events.append(ev("PartnerAchievementSubmitted", f"partner:{P2}", ref, at(date(2027, 6, 30), 13),
                                 title="Проплыл 400 м", criteria="Зачёт тренера", skill_ids=["activity.swimming"],
                                 program_id=prog["program_id"]))

    # Граничные случаи
    world = World(children, events, samples)
    dup = rnd.choice([e for e in events if e.event_type == "LessonCompleted"])
    events.append(dup.model_copy())
    world.edge_cases[str(dup.event_id)] = "дубликат: повторная доставка того же event_id"
    for env, what in [
        (ev("LessonCompleted", "online_school", "sch-unknown", at(date(2026, 11, 1)), lesson_id="x",
            course_id="course.python-start", duration_min=45), "неизвестный child_ref"),
        (Envelope(event_id=new_id(), event_type="TestPassed", source="online_school", child_ref="sch-masha",
                  occurred_at=at(date(2026, 11, 2)), payload={"test_id": "t", "course_id": "course.python-start",
                                                             "score_pct": 140}), "битый payload (140%)"),
        (ev("PartnerAchievementSubmitted", f"partner:{P1}", "sosny-0153", at(date(2027, 8, 1)),
            title="Турист года", criteria="—", skill_ids=["activity.outdoor"], program_id="partner.sosny.tourism"),
         "достижение партнёра без участия ребёнка в смене"),
    ]:
        events.append(env)
        world.edge_cases[str(env.event_id)] = what
    if forge:
        forged = {"title": "Пилот беспилотника — инструктор", "criteria": "—", "skill_ids": ["tech.drones"],
                  "program_id": "partner.sosny.drones"}
        when = at(date(2027, 7, 22))
        env = ev("PartnerAchievementSubmitted", f"partner:{P1}", "sosny-0153", when,
                 credential_jws=forge(forged, "sosny-0153", when), **forged)
        events.append(env)
        world.edge_cases[str(env.event_id)] = "бейдж партнёра с поддельной подписью"
    events.sort(key=lambda e: e.occurred_at)
    return world


def load(world: World, verifier=None) -> tuple[PassportService, dict]:
    """Регистрирует детей, оформляет согласия и прогоняет все события через сервис паспорта."""
    store, keys = Store(), KeyStore()
    consents = ConsentService(store)
    svc = PassportService(store, keys, consents, credential_verifier=verifier)
    ids = {}
    for c in world.children:
        cid = svc.register_child(c.birth_year, c.name, c.interests, c.external_ids, now=REGISTERED_AT)
        ids[id(c)] = cid
        for cons in c.consents:
            consents.grant(cid, c.guardian_sub, cons["purpose"], sms_code="0000",
                           data_categories=tuple(cons.get("data_categories", ())),
                           partner_issuer_id=cons.get("partner_issuer_id"), now=REGISTERED_AT)
    results = Counter()
    for e in world.events:
        r = svc.ingest(e)
        results[r.status if r.status != "rejected" else "rejected: " + r.reason.split(":")[0]] += 1
    hero_id = ids[id(next(c for c in world.children if c.hero))]
    return svc, {"results": results, "hero_id": hero_id}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--jsonl")
    args = ap.parse_args()
    from verify.badges import IssuerKey, IssuerRegistry, Verifier
    registry, keys = IssuerRegistry.demo()
    world = build(args.n, args.seed, sign=partner_signer(keys[P1]),
                  forge=partner_signer(IssuerKey.demo(P1, salt="attacker")))
    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as f:
            for e in world.events:
                f.write(e.model_dump_json() + "\n")
    verifier = Verifier(registry)
    svc, info = load(world, verifier=verifier.partner_hook())
    verifier.revoked = svc.store.revoked_credentials
    s = svc.store
    print(f"Детей: {len(world.children)}, событий: {len(world.events)}, "
          f"сырых сообщений телеметрии свёрнуто: {world.telemetry_samples:,}".replace(",", " "))
    print("\nРезультат приёма:")
    for k, v in sorted(info["results"].items()):
        print(f"  {k:<22} {v}")
    print("\nDLQ по причинам:", dict(Counter(d["kind"] for d in svc.dlq)))
    levels = Counter(r["level"] for rows in s.skills.values() for r in rows if r["valid_to"] is None)
    print("Текущие уровни навыков:", dict(sorted(levels.items())))
    print("Достижения по уровню доверия:",
          dict(sorted(Counter(a["trust_level"] for rows in s.achievements.values() for a in rows).items())))
    hid = info["hero_id"]
    print("\nПаспорт Маши: навыки")
    for r in svc.current_skills(hid)[:8]:
        print(f"  {r['name']:<40} ур. {r['level']}  очки {r['evidence_points']:>5}  доверие L{r['max_trust_level']}")
    print("Паспорт Маши: достижения")
    for a in s.achievements[hid]:
        print(f"  L{a['trust_level']}  {a['title']}  ({a['issuer_name']})")
    print("Активность Маши:", [(a["week_start"], a["activity_level"], a["regularity"]) for a in s.activity[hid]])


if __name__ == "__main__":
    main()
