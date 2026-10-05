-- LPCVC PhoneBench: tables, access lock and storage bucket.
--
-- Applied to the Supabase project by its GitHub integration when this file
-- reaches main; `python -m phonebench --env .env init-db` applies it by hand,
-- and the server tests apply it to a throwaway Postgres. Safe to run twice.
-- Local mode's SQLite schema is in server/phonebench/store.py; a test checks
-- that both have the same tables and columns.

CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  DOUBLE PRECISION NOT NULL,
    last_seen   DOUBLE PRECISION,
    status_json TEXT
);
CREATE TABLE IF NOT EXISTS members (
    email      TEXT PRIMARY KEY,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at DOUBLE PRECISION NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    upload_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    label          TEXT,
    uploaded_by    TEXT NOT NULL,
    created_at     DOUBLE PRECISION NOT NULL,
    max_edge       INTEGER NOT NULL,
    declared_bytes BIGINT NOT NULL,
    status         TEXT NOT NULL,
    path           TEXT,
    sha256         TEXT,
    bytes          BIGINT,
    width          INTEGER,
    height         INTEGER,
    source_sha256  TEXT,
    source_bytes   BIGINT,
    source_width   INTEGER,
    source_height  INTEGER,
    job_id         TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    submitted_by  TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    DOUBLE PRECISION NOT NULL,
    started_at    DOUBLE PRECISION,
    finished_at   DOUBLE PRECISION,
    outputs_at    DOUBLE PRECISION,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS job_images (
    image_id      TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    position      INTEGER NOT NULL,
    name          TEXT NOT NULL,
    label         TEXT,
    path          TEXT,
    sha256        TEXT NOT NULL,
    bytes         BIGINT NOT NULL,
    width         INTEGER NOT NULL,
    height        INTEGER NOT NULL,
    source_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prompts (
    prompt_id TEXT PRIMARY KEY,
    job_id    TEXT NOT NULL,
    position  INTEGER NOT NULL,
    text      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS units (
    unit_id       TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    model         TEXT NOT NULL,
    backend       TEXT NOT NULL,
    position      INTEGER NOT NULL,
    status        TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    device_id     TEXT,
    lease_expires DOUBLE PRECISION,
    created_at    DOUBLE PRECISION NOT NULL,
    assigned_at   DOUBLE PRECISION,
    finished_at   DOUBLE PRECISION,
    error         TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    unit_id     TEXT NOT NULL,
    model       TEXT NOT NULL,
    backend     TEXT NOT NULL,
    image_id    TEXT NOT NULL,
    prompt_id   TEXT NOT NULL,
    repetition  INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    status      TEXT NOT NULL,
    crashes     INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    reported_at DOUBLE PRECISION,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS units_by_status ON units (status, job_id, position);
CREATE INDEX IF NOT EXISTS runs_by_unit ON runs (unit_id, position);
CREATE INDEX IF NOT EXISTS runs_by_job ON runs (job_id, position);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS uploads_by_status ON uploads (status, created_at);

-- Supabase also serves the public schema through its REST API. Row level
-- security with no policies, plus no grants for the API's roles, leaves the
-- tables reachable only through the PhoneBench server, which owns them.
ALTER TABLE devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE members ENABLE ROW LEVEL SECURITY;
ALTER TABLE uploads ENABLE ROW LEVEL SECURITY;
ALTER TABLE jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_images ENABLE ROW LEVEL SECURITY;
ALTER TABLE prompts ENABLE ROW LEVEL SECURITY;
ALTER TABLE units ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'anon') THEN
        REVOKE ALL ON devices, members, uploads, jobs, job_images, prompts, units, runs FROM anon, authenticated;
    END IF;
END $$;

-- The private bucket: uploaded originals until they are prepared, prepared
-- images until their job is done, and result files too large to send
-- directly. Files are only reached through short-lived signed URLs.
DO $$
BEGIN
    IF to_regclass('storage.buckets') IS NOT NULL THEN
        INSERT INTO storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
        VALUES (
            'phonebench', 'phonebench', false, 33554432,
            ARRAY['image/png', 'image/jpeg', 'image/webp', 'text/csv', 'application/json', 'application/zip']
        )
        ON CONFLICT (id) DO UPDATE SET
            public = false,
            file_size_limit = EXCLUDED.file_size_limit,
            allowed_mime_types = EXCLUDED.allowed_mime_types;
    END IF;
END $$;
