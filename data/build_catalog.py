"""Собирает data/catalog.json: эмитенты и ~30 программ (смены, курсы, проектные треки).

Данные синтетические, названия программ придуманы для прототипа.
Запуск: python data/build_catalog.py
"""
import json
from pathlib import Path

ISSUERS = [
    {"issuer_id": "00000000-0000-4000-8000-000000000001", "name": "SportZania: лагеря", "type": "camp_core", "trust_tier": 2},
    {"issuer_id": "00000000-0000-4000-8000-000000000002", "name": "SportZania: онлайн-школа", "type": "online_school", "trust_tier": 2},
    {"issuer_id": "00000000-0000-4000-8000-000000000003", "name": "SportZania: проекты", "type": "projects", "trust_tier": 2},
    {"issuer_id": "00000000-0000-4000-8000-000000000101", "name": "Лагерь «Сосновый берег» (партнёр)", "type": "partner_camp", "trust_tier": 3},
    {"issuer_id": "00000000-0000-4000-8000-000000000102", "name": "Лагерь «Волна» (партнёр без ключа)", "type": "partner_camp", "trust_tier": 1},
]
CAMP, SCHOOL, PROJ, P1, P2 = (i["issuer_id"] for i in ISSUERS)

# (id, kind, title, issuer, skill_tags, target_level, age_min, age_max, format, start, end)
P = [
    ("shift.robots-ai", "shift", "Смена «ИИ и роботы»", CAMP, ["tech.robotics", "tech.ai", "social.teamwork"], 3, 10, 15, "offline", "2027-06-02", "2027-06-22"),
    ("shift.young-engineer", "shift", "Смена «Юный инженер»", CAMP, ["tech.electronics", "tech.3d", "science.physics"], 2, 8, 12, "offline", "2027-06-25", "2027-07-15"),
    ("shift.it-camp", "shift", "IT-смена «Код лета»", CAMP, ["tech.python", "tech.web", "social.project_mgmt"], 3, 12, 17, "offline", "2027-07-18", "2027-08-07"),
    ("shift.gamedev", "shift", "Смена «Создай свою игру»", CAMP, ["tech.gamedev", "creative.design", "social.teamwork"], 2, 10, 14, "offline", "2027-08-10", "2027-08-30"),
    ("shift.science-lab", "shift", "Смена «Научная лаборатория»", CAMP, ["science.research", "science.chemistry", "science.biology"], 2, 9, 14, "offline", "2027-06-02", "2027-06-22"),
    ("shift.media", "shift", "Медиасмена «В кадре»", CAMP, ["creative.video", "creative.writing", "social.presentation"], 2, 11, 17, "offline", "2027-07-18", "2027-08-07"),
    ("shift.sport-leaders", "shift", "Спортивная смена «Лидеры»", CAMP, ["activity.team_sports", "activity.endurance", "social.leadership"], 2, 10, 16, "offline", "2027-06-25", "2027-07-15"),
    ("shift.little-explorers", "shift", "Смена «Маленькие исследователи»", CAMP, ["science.biology", "creative.drawing", "activity.outdoor"], 1, 6, 9, "offline", "2027-06-02", "2027-06-15"),
    ("shift.startup", "shift", "Смена «Стартап-академия»", CAMP, ["social.entrepreneurship", "social.presentation", "social.project_mgmt"], 3, 14, 17, "offline", "2027-08-10", "2027-08-30"),
    ("shift.english-outdoor", "shift", "Языковая смена «English Outdoor»", CAMP, ["social.english", "activity.outdoor", "social.teamwork"], 2, 9, 15, "offline", "2027-07-18", "2027-08-07"),
    ("course.python-start", "course", "Python с нуля", SCHOOL, ["tech.python"], 2, 10, 17, "online", None, None),
    ("course.python-pro", "course", "Python: проекты и алгоритмы", SCHOOL, ["tech.python", "science.math"], 3, 12, 17, "online", None, None),
    ("course.scratch", "course", "Первые игры на Scratch", SCHOOL, ["tech.scratch", "tech.gamedev"], 2, 7, 11, "online", None, None),
    ("course.web", "course", "Сайты на HTML, CSS и JavaScript", SCHOOL, ["tech.web", "creative.design"], 2, 11, 17, "online", None, None),
    ("course.ai-intro", "course", "Введение в ИИ для школьников", SCHOOL, ["tech.ai", "science.math", "social.critical_thinking"], 2, 12, 17, "online", None, None),
    ("course.unity", "course", "Unity: 3D-игры", SCHOOL, ["tech.gamedev", "tech.3d"], 3, 12, 17, "online", None, None),
    ("course.math-olymp", "course", "Олимпиадная математика", SCHOOL, ["science.math", "social.critical_thinking"], 3, 10, 16, "online", None, None),
    ("course.english-speak", "course", "Разговорный английский", SCHOOL, ["social.english", "social.presentation"], 2, 8, 17, "online", None, None),
    ("course.design", "course", "Графический дизайн в Figma", SCHOOL, ["creative.design", "creative.drawing"], 2, 11, 17, "online", None, None),
    ("course.video", "course", "Монтаж видео для блога", SCHOOL, ["creative.video", "creative.writing"], 2, 11, 17, "online", None, None),
    ("course.robotics-home", "course", "Робототехника дома (Arduino)", SCHOOL, ["tech.robotics", "tech.electronics"], 2, 10, 15, "online", None, None),
    ("course.astronomy", "course", "Астрономия для любопытных", SCHOOL, ["science.astronomy", "science.physics"], 1, 8, 13, "online", None, None),
    ("project.smart-camp", "project_track", "Проект «Умный лагерь» (IoT)", PROJ, ["tech.electronics", "tech.python", "social.teamwork"], 3, 12, 17, "hybrid", None, None),
    ("project.eco", "project_track", "Проект «Экология нашего леса»", PROJ, ["science.biology", "science.research", "social.presentation"], 2, 9, 14, "hybrid", None, None),
    ("project.game-jam", "project_track", "Гейм-джем за выходные", PROJ, ["tech.gamedev", "creative.design", "social.teamwork"], 3, 11, 17, "online", None, None),
    ("project.chatbot", "project_track", "Чат-бот для смены", PROJ, ["tech.python", "tech.ai", "social.project_mgmt"], 3, 12, 17, "online", None, None),
    ("project.film", "project_track", "Короткометражный фильм", PROJ, ["creative.video", "creative.theatre", "social.leadership"], 2, 10, 17, "offline", None, None),
    ("project.business", "project_track", "Бизнес-идея за 4 недели", PROJ, ["social.entrepreneurship", "science.math", "social.presentation"], 3, 13, 17, "online", None, None),
    ("partner.sosny.drones", "shift", "Смена «Небо» (беспилотники)", P1, ["tech.drones", "tech.electronics", "social.teamwork"], 2, 11, 16, "offline", "2027-07-01", "2027-07-21"),
    ("partner.sosny.tourism", "shift", "Туристическая смена", P1, ["activity.outdoor", "activity.endurance", "social.leadership"], 2, 10, 16, "offline", "2027-08-01", "2027-08-21"),
    ("partner.volna.swim", "shift", "Смена «Пловец»", P2, ["activity.swimming", "activity.regularity"], 2, 8, 14, "offline", "2027-06-10", "2027-06-30"),
]

programs = [
    dict(zip(["program_id", "kind", "title", "issuer_id", "skill_tags", "target_level",
              "age_min", "age_max", "format", "starts_on", "ends_on"], row))
    for row in P
]

out = Path(__file__).with_name("catalog.json")
out.write_text(json.dumps({"issuers": ISSUERS, "programs": programs}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"{len(ISSUERS)} issuers, {len(programs)} programs -> {out}")
