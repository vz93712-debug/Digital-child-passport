"""Сквозное демо: год Маши (12–13 лет) через все механизмы паспорта.

  1. 200 детей и ~3000 событий из лагеря, онлайн-школы, проектов, браслетов и двух партнёров;
  2. паспорт Маши: навыки с уровнем доверия, достижения, недельная активность;
  3. бейдж партнёра «Сосновый берег» с проверенной подписью → L3, подделка → DLQ;
  4. экспорт бейджа «Python: уровень 3» в Open Badges 3.0, проверка, QR-код, попытка подмены;
  5. кто что видит: родитель, вожатый вне смены, партнёр по согласию; Маша скрывает достижение;
  6. рекомендации с объяснениями;
  7. отзыв согласия на активность;
  8. полное удаление по запросу родителя: бейдж отозван, бэкап нечитаем, акт без ПДн.

Запуск: python demo.py   (QR-код сохраняется в out/)
"""
from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from generator.generate import P1, build, load, partner_signer
from passport.access import (ChildContext, Consent, Participation, Viewer, VisibilityRule, can_set_visibility,
                             decide)
from privacy.deletion import DeletionService
from privacy.keys import KeyShredded
from recommender.engine import explain, recommend
from verify.badges import BadgeService, IssuerKey, IssuerRegistry, Verifier, b64u, decode, qr_png

TODAY = date(2027, 7, 25)
NOW = datetime(2027, 7, 25, 12, tzinfo=timezone.utc)
OUT = Path(__file__).resolve().parent / "out"


def h(title: str) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")


def context_of(svc, cid: str) -> ChildContext:
    s = svc.store
    return ChildContext(
        cid, s.children[cid]["age_band"],
        [Participation(p["program_id"], p["issuer_id"], date.fromisoformat(p["started_at"]),
                       date.fromisoformat(p["ended_at"]) if p["ended_at"] else None)
         for p in s.participations[cid]],
        [Consent(c["purpose"], frozenset(c["data_categories"]), c["partner_issuer_id"])
         for c in s.consents[cid] if c["revoked_at"] is None],
        [VisibilityRule(v["item_type"], v["item_id"], frozenset(v["hidden_from"]), v["set_by"])
         for v in s.visibility[cid]])


def main() -> None:
    registry, keys = IssuerRegistry.demo()
    verifier = Verifier(registry)
    world = build(200, 42, sign=partner_signer(keys[P1]), forge=partner_signer(IssuerKey.demo(P1, salt="attacker")))
    svc, info = load(world, verifier=verifier.partner_hook())
    s, masha = svc.store, info["hero_id"]
    badges = BadgeService(svc, keys)
    verifier.revoked, verifier.holder_name = s.revoked_credentials, badges.holder_name

    h("1. Платформа за год")
    r = info["results"]
    print(f"Детей {len(world.children)}, событий {len(world.events)}: принято {r['accepted']}, "
          f"дубликатов {r['duplicate']}, отклонено {sum(v for k, v in r.items() if k.startswith('rejected'))}")
    for eid, what in world.edge_cases.items():
        d = next((x for x in svc.dlq if x.get("event_id") == eid), None)
        status = f"DLQ ({d['kind']}): {d['reason']}" if d else "второй раз не учтён"
        print(f"  {what:<48} → {status}")

    h("2. Паспорт Маши")
    for row in svc.current_skills(masha)[:6]:
        print(f"  {row['name']:<40} ур. {row['level']}  доверие L{row['max_trust_level']}")
    print("  …")
    for a in s.achievements[masha]:
        print(f"  [L{a['trust_level']}] {a['title']:<42} {a['issuer_name']}")
    print("  активность по неделям:", ", ".join(f"{a['week_start']}: {a['activity_level']}" for a in s.activity[masha]))
    print("  уровень Python на 1 марта 2027:", svc.skill_level_at(masha, "tech.python", datetime(2027, 3, 1, tzinfo=timezone.utc)),
          "→ сейчас:", next(x["level"] for x in svc.current_skills(masha) if x["skill_id"] == "tech.python"))

    h("3. Бейдж партнёра: подпись проверена → L3")
    drone = next(a for a in s.achievements[masha] if a["issuer_id"] == P1)
    res = verifier.verify(drone["credential_jws"], NOW)
    print(f"  «{drone['title']}»: {res['status']}, проверки {res['checks']}, доверие L{drone['trust_level']}")
    print("  поддельный бейдж (тот же партнёр, чужой ключ):",
          next(d["reason"] for d in svc.dlq if d["kind"] == "credential"))

    h("4. Экспорт бейджа «Python: уровень 3» (Open Badges 3.0, Ed25519)")
    py = next(a for a in s.achievements[masha] if a["type"] == "skill_level" and "Python" in a["title"])
    exp = badges.export(masha, py["achievement_id"], NOW)
    header, cred = decode(exp["jws"])
    print("  заголовок JWS:", json.dumps(header, ensure_ascii=False))
    print("  тип:", cred["type"], " эмитент:", cred["issuer"]["name"])
    print("  ребёнок в бейдже:", cred["credentialSubject"]["identifier"][0]["identityHash"][:30] + "…  (имени нет)")
    print("  QR ведёт на:", exp["verify_url"])
    print("  QR сохранён:", qr_png(exp["verify_url"], OUT / "masha_python_badge.png").relative_to(OUT.parent))
    ok = verifier.verify(exp["jws"], NOW)
    print(f"  страница проверки: {ok['status']}, {ok['title']}, {ok['issuer_name']}, {ok['holder_display_name']}, "
          f"выдан {ok['issued_at']}, проверки {ok['checks']}")
    hh, pp, sig = exp["jws"].split(".")
    forged = dict(cred, name="Python: уровень 4")
    tampered = f"{hh}.{b64u(json.dumps(forged, ensure_ascii=False, separators=(',', ':')).encode())}.{sig}"
    t = verifier.verify(tampered, NOW)
    print(f"  подмена уровня 3 → 4 в бейдже: {t['status']} ({t['reason']})")

    h("5. Кто что видит")
    ctx = context_of(svc, masha)
    viewers = {
        "родитель": Viewer("guardian", "kc-parent-masha", guardian_of=frozenset({masha})),
        "вожатая смены «ИИ и роботы» (смена кончилась 22.06)": Viewer("counselor", "kc-counselor-olga",
                                                                       program_ids=frozenset({"shift.robots-ai"})),
        "партнёр «Сосновый берег»": Viewer("partner", "kc-partner-p1", issuer_id=P1),
    }
    for who, v in viewers.items():
        for cat in ("achievements", "skills", "observations"):
            d = decide(v, ctx, cat, today=TODAY)
            print(f"  {who:<52} {cat:<13} {'да' if d.allowed else 'нет: ' + d.reason}")
    shift = next(a for a in s.achievements[masha] if a["type"] == "shift_completion")
    print(f"\n  Маше 13 лет. Скрыть «{shift['title']}» от партнёра:",
          can_set_visibility(ctx.age_band, "child", frozenset({"partner"})))
    print("  Скрыть то же от родителя:", can_set_visibility(ctx.age_band, "child", frozenset({"guardian"})))
    s.visibility[masha].append({"item_type": "achievement", "item_id": shift["achievement_id"],
                                "hidden_from": ["partner"], "set_by": "child"})
    ctx = context_of(svc, masha)
    seen = [a["title"] for a in s.achievements[masha]
            if decide(viewers["партнёр «Сосновый берег»"], ctx, "achievements", item_id=a["achievement_id"],
                      today=TODAY).allowed]
    print(f"  партнёр теперь видит {len(seen)} из {len(s.achievements[masha])} достижений, без «{shift['title']}»")

    h("6. Рекомендации на конец июля 2027")
    rec = recommend(svc, masha, TODAY)
    for i, item in enumerate(rec["items"], 1):
        print(f"  {i}. " + explain(item).replace("\n", "\n     "))
    print("  отброшено правилами:", rec["filtered"])

    h("7. Родитель отзывает согласие на активность")
    before = s.footprint(masha)
    cons = next(c for c in s.consents[masha] if c["purpose"] == "activity" and c["revoked_at"] is None)
    rep = svc.consents.revoke(masha, cons["consent_id"], NOW)
    print(f"  очищено: {rep['purged']}; активность {before['activity']} → {s.footprint(masha)['activity']} недель")
    late = next(e for e in world.events if e.event_type == "ActivityWeeklySummarized" and e.child_ref == "br-masha")
    late = late.model_copy(update={"event_id": uuid.uuid4()})
    print("  новая сводка браслета:", svc.ingest(late).reason)

    h("8. Родитель удаляет паспорт полностью")
    backup = s.snapshot()
    deletion = DeletionService(s, svc.keys, pseudonym_secret=b"demo-secret")
    req = deletion.execute(deletion.request(masha, "kc-parent-masha", NOW), NOW)
    print("  шаги:", " → ".join(req.steps_done))
    print("  что осталось в паспорте:", {k: v for k, v in s.footprint(masha).items() if v} or "ничего")
    v = verifier.verify(exp["jws"], NOW)
    print(f"  QR бейджа Python теперь показывает: {v['status']} ({v['reason']}), имя: {v.get('holder_display_name', '—')}")
    try:
        svc.keys.decrypt(masha, backup.events[masha][0]["payload_enc"])
    except KeyShredded:
        print("  журнал в бэкапе: ключ уничтожен, расшифровать нельзя")
    print("  партнёрам ушло:", [o for o in s.outbox if o.get("event_type") == "ChildDataDeletionRequested"])
    print("  акт:", req.act)


if __name__ == "__main__":
    main()
