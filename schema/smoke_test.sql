-- Smoke test схемы: вставка минимального паспорта и проверка ключевых ограничений.
-- Запуск: psql -v ON_ERROR_STOP=1 -f ddl.sql -f smoke_test.sql
BEGIN;
INSERT INTO passport.issuer (issuer_id, name, type, trust_tier)
VALUES ('00000000-0000-0000-0000-000000000001', 'SportZania Лагерь', 'camp_core', 2);
INSERT INTO passport.child (child_id, birth_year, age_band, display_name, interests, key_id)
VALUES ('10000000-0000-0000-0000-000000000001', 2014, '10-13', 'Маша', '{robotics,python}', gen_random_uuid());
INSERT INTO passport.skill VALUES ('tech', 'Технологии', 'tech', NULL, NULL), ('tech.python', 'Python', 'tech', 'tech', NULL);
INSERT INTO passport.child_skill (child_id, skill_id, level, max_trust_level, valid_from)
VALUES ('10000000-0000-0000-0000-000000000001', 'tech.python', 2, 1, now());
COMMIT;

-- 1. Уровень 3 без подтверждения человека должен отклоняться.
DO $$ BEGIN
  INSERT INTO passport.child_skill (child_id, skill_id, level, max_trust_level, valid_from)
  VALUES ('10000000-0000-0000-0000-000000000001', 'tech.python', 3, 1, now() + interval '1 day');
  RAISE EXCEPTION 'FAIL: level 3 accepted without mentor';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: level 3 requires trust >= 2';
END $$;

-- 2. Две «текущие» версии навыка недопустимы.
DO $$ BEGIN
  INSERT INTO passport.child_skill (child_id, skill_id, level, max_trust_level, valid_from)
  VALUES ('10000000-0000-0000-0000-000000000001', 'tech.python', 2, 1, now() + interval '2 day');
  RAISE EXCEPTION 'FAIL: two current versions';
EXCEPTION WHEN unique_violation THEN RAISE NOTICE 'OK: one current version per skill';
END $$;

-- 3. Журнал событий нельзя изменить.
INSERT INTO passport.passport_event (event_id, child_id, event_type, schema_version, source, occurred_at, payload_enc)
VALUES (gen_random_uuid(), '10000000-0000-0000-0000-000000000001', 'LessonCompleted', 1, 'online_school', now(), '\x00');
DO $$ BEGIN
  UPDATE passport.passport_event SET event_type = 'X';
  RAISE EXCEPTION 'FAIL: event updated';
EXCEPTION WHEN raise_exception THEN
  IF SQLERRM LIKE '%append-only%' THEN RAISE NOTICE 'OK: passport_event is append-only'; ELSE RAISE; END IF;
END $$;

-- 4. Партнёр уровня 3 без публичного ключа недопустим.
DO $$ BEGIN
  INSERT INTO passport.issuer (name, type, trust_tier) VALUES ('Лагерь без ключа', 'partner_camp', 3);
  RAISE EXCEPTION 'FAIL: tier 3 without key';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: tier 3 issuer needs public key';
END $$;

-- 5. Согласие на передачу партнёру без указания партнёра недопустимо.
DO $$ BEGIN
  INSERT INTO passport.guardian (guardian_id, keycloak_sub) VALUES ('20000000-0000-0000-0000-000000000001', 'kc-parent-1');
  INSERT INTO passport.consent (child_id, guardian_id, purpose, data_categories, text_version, document_ref, granted_at)
  VALUES ('10000000-0000-0000-0000-000000000001', '20000000-0000-0000-0000-000000000001', 'share_partner', '{achievements}', 'v1', 'doc', now());
  RAISE EXCEPTION 'FAIL: share_partner without partner';
EXCEPTION WHEN check_violation THEN RAISE NOTICE 'OK: share_partner requires partner';
END $$;

-- 6. Удаление ребёнка каскадно стирает паспорт.
DELETE FROM passport.child WHERE child_id = '10000000-0000-0000-0000-000000000001';
DO $$ BEGIN
  IF (SELECT count(*) FROM passport.passport_event) = 0 AND (SELECT count(*) FROM passport.child_skill) = 0
  THEN RAISE NOTICE 'OK: cascade delete'; ELSE RAISE EXCEPTION 'FAIL: cascade'; END IF;
END $$;
