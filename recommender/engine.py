"""Рекомендатель v1: жёсткие правила + взвешенная оценка с объяснением каждой рекомендации.

Почему не ML сразу: на старте нет истории откликов на рекомендации, а объяснимость — требование
кейса и родителей. v1 даёт понятные причины и копит отклики (показ → клик → бронь), на которых
потом обучается гибрид LightFM (контент + коллаборативный сигнал). Формула и веса — в WEIGHTS.

Шаг 1. Правила (кандидат отбрасывается, причина считается в filtered):
  * нет согласия «recommendations» → рекомендаций нет вообще;
  * возраст вне диапазона программы; программа уже прошла (смена началась раньше today);
  * ребёнок уже участвовал; программа не даёт роста ни по одному навыку (цель ≤ текущего уровня).
  Начатый и не законченный курс не отбрасывается, а поднимается наверх правилом «доведи до конца».

Шаг 2. Оценка, все компоненты в [0, 1]:
  interest  — доля навыков программы среди интересов ребёнка;
  growth    — насколько цель программы — «следующая ступень» по навыкам (ур.+1 лучше, чем +2 и +3);
  evidence  — есть ли на что опереться: накопленные очки по навыкам программы;
  novelty   — доля новых для ребёнка навыков (расширяет кругозор, противовес evidence);
  social    — доля ребят того же возраста с похожим опытом, которые прошли программу.

Шаг 3. Разнообразие: не больше двух программ одного вида (смена, курс, проект) в выдаче.
Данные: только проекции паспорта. Сырые события, отзывы наставников и браслет не читаются.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

MODEL_VERSION = "rules-v1"
WEIGHTS = {"interest": 0.35, "growth": 0.30, "evidence": 0.15, "novelty": 0.10, "social": 0.10}
MAX_PER_KIND = 2
KIND_RU = {"shift": "смена", "course": "курс", "project_track": "проект"}
GROWTH_BY_GAP = {1: 1.0, 2: 0.6, 3: 0.3, 4: 0.2}


@dataclass
class Recommendation:
    program_id: str
    title: str
    kind: str
    score: float
    components: dict[str, float]
    reasons: list[str]
    rule: Optional[str] = None

    def as_row(self, generated_at: datetime) -> dict:
        return {"program_id": self.program_id, "score": round(self.score, 3), "components": self.components,
                "reasons": self.reasons, "rule": self.rule, "model_version": MODEL_VERSION,
                "generated_at": generated_at.isoformat()}


def _levels(svc, cid: str) -> dict[str, dict]:
    return {r["skill_id"]: r for r in svc.current_skills(cid)}


def _taken(svc, cid: str) -> dict[str, str]:
    return {p["program_id"]: p["status"] for p in svc.store.participations.get(cid, []) if p["status"] != "cancelled"}


def _peers(svc, cid: str, band: str, mine: set[str]) -> list[str]:
    """Похожие ребята: та же возрастная группа и хотя бы одна общая завершённая программа."""
    out = []
    for other, child in svc.store.children.items():
        if other == cid or child["age_band"] != band:
            continue
        done = {p for p, st in _taken(svc, other).items() if st == "completed"}
        if done & mine:
            out.append(other)
    return out


def recommend(svc, cid: str, today: date, k: int = 5, save: bool = True) -> dict:
    child = svc.store.children[cid]
    if not svc.consents.has(cid, "recommendations"):
        return {"items": [], "filtered": {"нет согласия «recommendations»": len(svc.programs)}}
    age = today.year - child["birth_year"]
    levels, taken = _levels(svc, cid), _taken(svc, cid)
    interests = set(child["interests"])
    mine_done = {p for p, st in taken.items() if st == "completed"}
    peers = _peers(svc, cid, child["age_band"], mine_done)
    peer_done = Counter(p for o in peers for p, st in _taken(svc, o).items() if st == "completed")
    thresholds = {lvl: c["min_points"] for lvl, c in svc.rules["levels"].items()}
    name = svc.skill_names.get

    filtered: Counter = Counter()
    scored: list[Recommendation] = []
    for prog in svc.programs.values():
        pid, tags, target = prog["program_id"], prog["skill_tags"], prog["target_level"]
        if not prog["age_min"] <= age <= prog["age_max"]:
            filtered["не подходит по возрасту"] += 1
            continue
        if prog.get("starts_on") and date.fromisoformat(prog["starts_on"]) < today:
            filtered["уже началась или прошла"] += 1
            continue
        if pid in taken and not (prog["kind"] == "course" and taken[pid] == "active"):
            filtered["уже участвовал(а)"] += 1
            continue
        lvl = {t: levels.get(t, {}).get("level", 0) for t in tags}
        gaps = {t: target - lvl[t] for t in tags}
        if pid not in taken and all(g <= 0 for g in gaps.values()):
            filtered["нет роста: цель не выше текущих уровней"] += 1
            continue

        pts = {t: levels.get(t, {}).get("evidence_points", 0.0) for t in tags}
        comp = {
            "interest": len(set(tags) & interests) / len(tags),
            "growth": sum(GROWTH_BY_GAP.get(g, 0.0) for g in gaps.values()) / len(tags),
            "evidence": sum(min(pts[t] / thresholds[max(1, min(target, 4))], 1.0) for t in tags) / len(tags),
            "novelty": sum(1 for t in tags if pts[t] == 0) / len(tags),
            "social": peer_done[pid] / len(peers) if peers else 0.0,
        }
        score = sum(WEIGHTS[c] * v for c, v in comp.items())
        rule = None
        if taken.get(pid) == "active":
            rule, score = "unfinished_course", 1.0 + score

        reasons: list[str] = []
        if rule:
            reasons.append("Курс начат, но не закончен: осталось довести до конца")
        contrib = sorted(((WEIGHTS[c] * v, c) for c, v in comp.items() if v > 0), reverse=True)
        for _, c in contrib:
            if c == "interest":
                reasons.append("Совпадает с интересами: " + ", ".join(name(t, t) for t in tags if t in interests))
            elif c == "growth":
                steps = [f"{name(t, t)}: ур. {lvl[t]} → {target}" for t in tags if gaps[t] in (1, 2)]
                if steps:
                    reasons.append("Следующая ступень: " + ", ".join(steps))
            elif c == "evidence":
                base = [name(t, t) for t in tags if pts[t] > 0]
                reasons.append("Опирается на уже доказанное: " + ", ".join(base))
            elif c == "novelty" and comp["novelty"] < 1:
                reasons.append("Новое направление: " + ", ".join(name(t, t) for t in tags if pts[t] == 0))
            elif c == "novelty":
                reasons.append("Полностью новое направление для ребёнка")
            elif c == "social" and peer_done[pid] >= 2:
                reasons.append(f"Эту программу прошли {peer_done[pid]} из {len(peers)} ребят того же возраста с похожим опытом")
        scored.append(Recommendation(pid, prog["title"], prog["kind"], score,
                                     {c: round(v, 2) for c, v in comp.items()}, reasons[:3], rule))

    scored.sort(key=lambda r: -r.score)
    items, per_kind = [], Counter()
    for r in scored:
        if per_kind[r.kind] >= MAX_PER_KIND and r.rule is None:
            continue
        items.append(r)
        per_kind[r.kind] += 1
        if len(items) == k:
            break
    if save:
        now = datetime.combine(today, datetime.min.time(), timezone.utc)
        svc.store.recommendations[cid] = [r.as_row(now) for r in items]
    return {"items": items, "filtered": dict(filtered), "peers": len(peers)}


def explain(r: Recommendation) -> str:
    """Текст для карточки в приложении родителя."""
    kind = KIND_RU.get(r.kind, r.kind)
    lines = [f"{r.title} ({kind}), оценка {r.score:.2f}"]
    lines += [f"  · {x}" for x in r.reasons]
    return "\n".join(lines)
