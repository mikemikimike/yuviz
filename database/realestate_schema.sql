-- Real-estate sales schema: projects, site visits, leads and callback requests.
-- Run: psql voiceai -f database/realestate_schema.sql (after schema.sql, before rls.sql). Idempotent.
--
-- These tables are the source of truth for site-visit availability and for the sales team's
-- follow-up list. The realestate service's tool API is the only writer during calls.

-- ── re_settings — one row per tenant that sells property ────────────────────
CREATE TABLE IF NOT EXISTS re_settings (
    tenant_id            UUID PRIMARY KEY REFERENCES tenants(id),
    company_name         TEXT NOT NULL,
    company_name_hi      TEXT,
    sales_phone          TEXT,
    timezone             TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    -- Visits run on these weekdays (ISO: 1 = Monday .. 7 = Sunday), in whole slots between the two hours.
    visit_days           INT[] NOT NULL DEFAULT '{1,2,3,4,5,6}',
    visit_start_hour     INT NOT NULL DEFAULT 10 CHECK (visit_start_hour BETWEEN 0 AND 23),
    visit_end_hour       INT NOT NULL DEFAULT 18 CHECK (visit_end_hour BETWEEN 1 AND 24),
    visit_slot_minutes   INT NOT NULL DEFAULT 60 CHECK (visit_slot_minutes BETWEEN 15 AND 240),
    -- Visits that can be booked for one project at the same time (executives on site).
    visit_capacity       INT NOT NULL DEFAULT 2 CHECK (visit_capacity BETWEEN 1 AND 50),
    booking_window_days  INT NOT NULL DEFAULT 14 CHECK (booking_window_days BETWEEN 1 AND 60),
    min_lead_minutes     INT NOT NULL DEFAULT 120 CHECK (min_lead_minutes >= 0),
    max_active_per_phone INT NOT NULL DEFAULT 3 CHECK (max_active_per_phone >= 1),
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (visit_end_hour > visit_start_hour)
);

-- ── re_api_keys — how toolexec's calls prove which tenant they are for ──────
-- Only the SHA-256 of the key is stored; the key itself lives in custom_apis.auth_config as an enc: ref.
CREATE TABLE IF NOT EXISTS re_api_keys (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    key_hash    TEXT NOT NULL UNIQUE,
    label       TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at  TIMESTAMPTZ
);

-- ── re_projects ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   UUID NOT NULL REFERENCES tenants(id),
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    name_hi     TEXT,
    locality    TEXT NOT NULL DEFAULT '',
    kind        TEXT NOT NULL DEFAULT 'apartment' CHECK (kind IN ('apartment', 'villa', 'commercial')),
    status      TEXT NOT NULL DEFAULT 'under_construction'
                CHECK (status IN ('under_construction', 'ready_to_move', 'sold_out')),
    summary     TEXT NOT NULL DEFAULT '',
    -- Where visitors are met, quoted in the confirmation.
    meeting_point TEXT NOT NULL DEFAULT '',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at  TIMESTAMPTZ,
    UNIQUE (tenant_id, code)
);

-- ── re_site_visits ──────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS re_site_visits (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenants(id),
    reference        TEXT NOT NULL,
    project_id       UUID NOT NULL REFERENCES re_projects(id),
    visitor_name     TEXT NOT NULL,
    visitor_phone    TEXT NOT NULL,
    booked_by_phone  TEXT,
    starts_at        TIMESTAMPTZ NOT NULL,
    ends_at          TIMESTAMPTZ NOT NULL,
    status           TEXT NOT NULL DEFAULT 'booked' CHECK (status IN ('booked', 'cancelled', 'completed', 'no_show')),
    notes            TEXT NOT NULL DEFAULT '',
    language         TEXT NOT NULL DEFAULT 'en',
    channel          TEXT NOT NULL DEFAULT 'phone',
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at     TIMESTAMPTZ,
    UNIQUE (tenant_id, reference)
);
CREATE INDEX IF NOT EXISTS re_site_visits_slot_idx ON re_site_visits (tenant_id, project_id, starts_at) WHERE status = 'booked';
CREATE INDEX IF NOT EXISTS re_site_visits_phone_idx ON re_site_visits (tenant_id, visitor_phone) WHERE status = 'booked';

-- ── re_leads — one row per phone number, updated as the caller reveals more ──
CREATE TABLE IF NOT EXISTS re_leads (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID NOT NULL REFERENCES tenants(id),
    phone          TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    project_code   TEXT,
    property_type  TEXT NOT NULL DEFAULT '',
    budget         TEXT NOT NULL DEFAULT '',
    timeline       TEXT NOT NULL DEFAULT '',
    notes          TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'contacted', 'qualified', 'closed')),
    source         TEXT NOT NULL DEFAULT 'phone',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, phone)
);

-- ── re_callbacks — "have a sales executive call me" ──────────────────────────
CREATE TABLE IF NOT EXISTS re_callbacks (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    reference       TEXT NOT NULL,
    phone           TEXT NOT NULL,
    name            TEXT NOT NULL DEFAULT '',
    preferred_time  TEXT NOT NULL DEFAULT '',
    topic           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'cancelled')),
    language        TEXT NOT NULL DEFAULT 'en',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at         TIMESTAMPTZ,
    UNIQUE (tenant_id, reference)
);
CREATE INDEX IF NOT EXISTS re_callbacks_pending_idx ON re_callbacks (tenant_id, created_at) WHERE status = 'pending';
