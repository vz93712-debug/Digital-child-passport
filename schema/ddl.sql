-- Цифровой паспорт ребёнка: модель данных (PostgreSQL 14+)
--
-- Три схемы:
--   passport — содержимое паспорта, только псевдонимные данные;
--   pii      — ФИО и точные даты, отдельная БД/кластер в продакшене, всё зашифровано;
--   audit    — журнал доступа к чужим паспортам.
--
-- Принципы:
--   * passport.passport_event — append-only журнал, источник истины; остальные таблицы passport — проекции.
--   * Персональные данные в журнале (payload_enc) шифруются ключом ребёнка (crypto-shredding).
--   * Данных о здоровье и геопозиции в паспорте нет: они живут в модуле лагеря и контуре телеметрии.

CREATE EXTENSION IF NOT EXISTS pgcrypto;  -- gen_random_uuid()

CREATE SCHEMA IF NOT EXISTS passport;
CREATE SCHEMA IF NOT EXISTS pii;
CREATE SCHEMA IF NOT EXISTS audit;

-- ─────────────────────────────────────────────────────────────
-- 1. Ядро и личность
-- ─────────────────────────────────────────────────────────────

CREATE TABLE passport.child (
    child_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    birth_year    smallint    NOT NULL CHECK (birth_year BETWEEN 2000 AND 2100),
    age_band      text        NOT NULL CHECK (age_band IN ('4-9', '10-13', '14-17', '18+')),
    display_name  text,                      -- имя без фамилии, для интерфейса и бейджей
    interests     text[]      NOT NULL DEFAULT '{}',   -- теги из taxonomy (data/skills.yaml)
    status        text        NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'blocked', 'deletion_pending', 'deleted')),
    key_id        uuid        NOT NULL,      -- ключ ребёнка в хранилище ключей (crypto-shredding)
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

-- ФИО и точная дата рождения: в продакшене — отдельный кластер, доступ только сервису профилей.
CREATE TABLE pii.child_pii (
    child_id        uuid PRIMARY KEY REFERENCES passport.child (child_id) ON DELETE CASCADE,
    full_name_enc   bytea NOT NULL,
    birth_date_enc  bytea NOT NULL,
    key_id          uuid  NOT NULL
);

CREATE TABLE passport.guardian (
    guardian_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    keycloak_sub  text UNIQUE NOT NULL,      -- контакты хранит Keycloak, не паспорт
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE passport.child_guardian (
    child_id     uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    guardian_id  uuid NOT NULL REFERENCES passport.guardian (guardian_id) ON DELETE CASCADE,
    relation     text NOT NULL CHECK (relation IN ('parent', 'guardian', 'adoptive_parent')),
    verified_at  timestamptz NOT NULL,       -- подтверждение законного представителя
    PRIMARY KEY (child_id, guardian_id)
);

-- Эмитенты: модули платформы и лагеря-партнёры.
CREATE TABLE passport.issuer (
    issuer_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name          text NOT NULL,
    type          text NOT NULL CHECK (type IN ('camp_core', 'online_school', 'projects', 'partner_camp')),
    trust_tier    smallint NOT NULL CHECK (trust_tier BETWEEN 1 AND 3),
                  -- 1: данные без подписи, 2: модуль SportZania, 3: аккредитованный партнёр с ключом
    public_key    text,                      -- multibase Ed25519, обязателен для tier 3
    did           text,                      -- идентификатор эмитента в Open Badges 3.0
    status        text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended', 'revoked')),
    created_at    timestamptz NOT NULL DEFAULT now(),
    CHECK (trust_tier < 3 OR public_key IS NOT NULL)
);

-- Склейка профилей: один ребёнок — несколько ID в разных системах.
CREATE TABLE passport.external_identity (
    child_id       uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    source_system  text NOT NULL,            -- camp_core | online_school | partner:<issuer_id>
    external_id    text NOT NULL,
    link_method    text NOT NULL CHECK (link_method IN ('guardian_account', 'link_code', 'migration_match', 'manual')),
    linked_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, external_id)
);
CREATE INDEX ON passport.external_identity (child_id);

-- ─────────────────────────────────────────────────────────────
-- 2. Содержание паспорта
-- ─────────────────────────────────────────────────────────────

-- Каталог: смены, курсы, проектные треки (data/catalog.json).
CREATE TABLE passport.program (
    program_id   text PRIMARY KEY,
    kind         text NOT NULL CHECK (kind IN ('shift', 'course', 'project_track')),
    title        text NOT NULL,
    issuer_id    uuid NOT NULL REFERENCES passport.issuer (issuer_id),
    skill_tags   text[] NOT NULL,
    target_level smallint NOT NULL CHECK (target_level BETWEEN 1 AND 4),
    age_min      smallint NOT NULL,
    age_max      smallint NOT NULL,
    format       text NOT NULL CHECK (format IN ('offline', 'online', 'hybrid')),
    CHECK (age_min <= age_max)
);

-- Таксономия навыков (data/skills.yaml): дерево, опционально связано с ESCO.
CREATE TABLE passport.skill (
    skill_id   text PRIMARY KEY,             -- например 'tech.python'
    name       text NOT NULL,
    category   text NOT NULL CHECK (category IN ('tech', 'creative', 'social', 'activity', 'science')),
    parent_id  text REFERENCES passport.skill (skill_id),
    esco_uri   text
);

CREATE TABLE passport.participation (
    participation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    child_id         uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    program_id       text NOT NULL REFERENCES passport.program (program_id),
    issuer_id        uuid NOT NULL REFERENCES passport.issuer (issuer_id),
    started_at       date NOT NULL,
    ended_at         date,
    status           text NOT NULL CHECK (status IN ('planned', 'active', 'completed', 'cancelled')),
    source_event_id  uuid NOT NULL,          -- событие, которое создало запись
    CHECK (ended_at IS NULL OR ended_at >= started_at)
);
CREATE INDEX ON passport.participation (child_id);

-- Уровень навыка с историей (SCD2): текущая версия — valid_to IS NULL.
CREATE TABLE passport.child_skill (
    child_id        uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    skill_id        text NOT NULL REFERENCES passport.skill (skill_id),
    level           smallint NOT NULL CHECK (level BETWEEN 0 AND 4),
                    -- 1 познакомился, 2 практикует, 3 применяет (нужна подпись наставника), 4 учит других
    evidence_points numeric(6,2) NOT NULL DEFAULT 0,
    evidence_count  integer NOT NULL DEFAULT 0,
    max_trust_level smallint NOT NULL CHECK (max_trust_level BETWEEN 0 AND 3),
    valid_from      timestamptz NOT NULL,
    valid_to        timestamptz,
    PRIMARY KEY (child_id, skill_id, valid_from),
    CHECK (valid_to IS NULL OR valid_to > valid_from),
    CHECK (level < 3 OR max_trust_level >= 2)   -- уровни 3–4 только с подтверждением человека
);
CREATE UNIQUE INDEX child_skill_current ON passport.child_skill (child_id, skill_id) WHERE valid_to IS NULL;

CREATE TABLE passport.achievement (
    achievement_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    child_id         uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    type             text NOT NULL CHECK (type IN ('course_completion', 'shift_completion', 'project', 'skill_level', 'award', 'self_declared')),
    title            text NOT NULL,
    criteria         text NOT NULL,          -- за что выдано, человекочитаемо (OB3: criteria.narrative)
    issuer_id        uuid REFERENCES passport.issuer (issuer_id),   -- NULL только для L0
    signer_id        text,                   -- keycloak_sub наставника для L2
    trust_level      smallint NOT NULL CHECK (trust_level BETWEEN 0 AND 3),
    skill_ids        text[] NOT NULL DEFAULT '{}',
    evidence_refs    text[] NOT NULL DEFAULT '{}',   -- participation_id, project_id, URL результата
    participation_id uuid REFERENCES passport.participation (participation_id),
    issued_at        timestamptz NOT NULL,
    revoked_at       timestamptz,
    revocation_reason text,
    credential_jws   text,                   -- подписанный Open Badge 3.0 (для L1–L3)
    credential_id    text UNIQUE,            -- id бейджа (urn:uuid:…): по нему ищет страница проверки из QR
    exported_at      timestamptz,            -- когда бейдж выдан наружу (нужно согласие export)
    status_list_index integer UNIQUE,        -- позиция в списке отзыва (Bitstring Status List)
    CHECK (trust_level = 0 OR issuer_id IS NOT NULL),
    CHECK (trust_level <> 2 OR signer_id IS NOT NULL),
    CHECK (revoked_at IS NULL OR revocation_reason IS NOT NULL)
);
CREATE INDEX ON passport.achievement (child_id) WHERE revoked_at IS NULL;

CREATE TABLE passport.project_passport (
    project_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    title        text NOT NULL,
    goal         text NOT NULL,
    result_url   text,
    program_id   text REFERENCES passport.program (program_id),
    mentor_sub   text NOT NULL,
    started_at   date NOT NULL,
    finished_at  date,
    status       text NOT NULL CHECK (status IN ('in_progress', 'submitted', 'reviewed'))
);

CREATE TABLE passport.project_member (
    project_id    uuid NOT NULL REFERENCES passport.project_passport (project_id) ON DELETE CASCADE,
    child_id      uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    role          text NOT NULL,             -- капитан, программист, дизайнер, исследователь...
    contribution  text,
    PRIMARY KEY (project_id, child_id)
);

CREATE TABLE passport.mentor_observation (
    observation_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    child_id         uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    author_sub       text NOT NULL,
    participation_id uuid NOT NULL REFERENCES passport.participation (participation_id),
    text_enc         bytea NOT NULL,         -- свободный текст может содержать ПДн → шифруется ключом ребёнка
    skill_ids        text[] NOT NULL DEFAULT '{}',
    signed_at        timestamptz NOT NULL,
    signature        text NOT NULL
);
CREATE INDEX ON passport.mentor_observation (child_id);

-- Навыки активности: только дискретные недельные уровни, без координат и пульса.
CREATE TABLE passport.activity_summary (
    child_id          uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    week_start        date NOT NULL,
    activity_level    text NOT NULL CHECK (activity_level IN ('low', 'medium', 'high')),
    regularity        text NOT NULL CHECK (regularity IN ('irregular', 'regular', 'very_regular')),
    sleep_regularity  text CHECK (sleep_regularity IN ('irregular', 'regular')),
    PRIMARY KEY (child_id, week_start)
);

-- Сохранённые рекомендации (только при согласии recommendations; отзыв согласия удаляет строки).
CREATE TABLE passport.recommendation (
    child_id       uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    program_id     text NOT NULL REFERENCES passport.program (program_id),
    rank           smallint NOT NULL CHECK (rank >= 1),
    score          numeric(5, 3) NOT NULL,
    components     jsonb NOT NULL,          -- interest, growth, evidence, novelty, social
    reasons        text[] NOT NULL CHECK (cardinality(reasons) >= 1),   -- без объяснения не показываем
    model_version  text NOT NULL,           -- rules-v1, позже lightfm-…
    generated_at   timestamptz NOT NULL,
    shown_at       timestamptz,             -- отклики копятся для обучения следующей модели
    clicked_at     timestamptz,
    booked_at      timestamptz,
    PRIMARY KEY (child_id, generated_at, rank)
);

-- ─────────────────────────────────────────────────────────────
-- 3. Приватность и служебное
-- ─────────────────────────────────────────────────────────────

-- Согласие законного представителя — отдельно на каждую цель.
CREATE TABLE passport.consent (
    consent_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    child_id         uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    guardian_id      uuid NOT NULL REFERENCES passport.guardian (guardian_id),
    purpose          text NOT NULL CHECK (purpose IN ('passport', 'activity', 'recommendations', 'share_partner', 'export')),
    partner_issuer_id uuid REFERENCES passport.issuer (issuer_id),   -- для share_partner: с кем
    data_categories  text[] NOT NULL,
    text_version     text NOT NULL,          -- версия текста согласия, который подписан
    document_ref     text NOT NULL,          -- ссылка на подписанный документ (ПЭП: код из SMS + хэш текста)
    granted_at       timestamptz NOT NULL,
    expires_at       timestamptz,
    revoked_at       timestamptz,
    CHECK (purpose <> 'share_partner' OR partner_issuer_id IS NOT NULL),
    CHECK (revoked_at IS NULL OR revoked_at >= granted_at)
);
CREATE INDEX consent_active ON passport.consent (child_id, purpose) WHERE revoked_at IS NULL;

-- «Право ребёнка скрыть»: элемент остаётся, меняется видимость.
CREATE TABLE passport.visibility_rule (
    child_id     uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    item_type    text NOT NULL CHECK (item_type IN ('achievement', 'observation', 'project', 'skill', 'activity')),
    item_id      text NOT NULL,
    hidden_from  text[] NOT NULL,            -- роли: partner, teacher, counselor, export, guardian
    set_by       text NOT NULL CHECK (set_by IN ('child', 'guardian')),
    set_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (child_id, item_type, item_id)
);

-- Журнал событий паспорта: append-only, источник истины для всех проекций.
CREATE TABLE passport.passport_event (
    event_id        uuid PRIMARY KEY,        -- из конверта источника → дедупликация
    child_id        uuid NOT NULL REFERENCES passport.child (child_id) ON DELETE CASCADE,
    event_type      text NOT NULL,
    schema_version  smallint NOT NULL,
    source          text NOT NULL,
    occurred_at     timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    payload_enc     bytea NOT NULL,          -- AES-GCM ключом ребёнка
    seq             bigserial UNIQUE         -- порядок применения к проекциям
);
CREATE INDEX ON passport.passport_event (child_id, occurred_at);

-- Запрет на изменение журнала (удаление — только каскадом при уничтожении ребёнка).
CREATE FUNCTION passport.forbid_event_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'passport_event is append-only';
END $$;
CREATE TRIGGER passport_event_append_only
    BEFORE UPDATE ON passport.passport_event
    FOR EACH ROW EXECUTE FUNCTION passport.forbid_event_update();

CREATE TABLE passport.deletion_request (
    request_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    child_id       uuid NOT NULL,            -- без FK: запись переживает удаление ребёнка
    guardian_id    uuid NOT NULL,
    requested_at   timestamptz NOT NULL DEFAULT now(),
    deadline_at    timestamptz NOT NULL,     -- requested_at + 30 дней (ст. 21 ч. 5 152-ФЗ), ставит сервис
    CHECK (deadline_at <= requested_at + interval '30 days'),
    completed_at   timestamptz,
    steps_done     text[] NOT NULL DEFAULT '{}',   -- blocked, revoked_badges, projections, key_shredded, analytics, partners_notified
    act_ref        text                      -- акт об уничтожении
);

-- Аудит: кто и зачем открывал чужой паспорт.
CREATE TABLE audit.access_log (
    id          bigserial PRIMARY KEY,
    actor_sub   text NOT NULL,
    actor_role  text NOT NULL,
    child_id    uuid NOT NULL,
    action      text NOT NULL,
    purpose     text NOT NULL,
    at          timestamptz NOT NULL DEFAULT now()
);
