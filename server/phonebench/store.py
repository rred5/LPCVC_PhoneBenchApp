"""SQLite storage for local mode. Phase 2 moves the same tables to Supabase."""

from contextlib import contextmanager
import hashlib
import hmac
import secrets
import sqlite3
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    last_seen   REAL,
    status_json TEXT
);
CREATE TABLE IF NOT EXISTS uploads (
    upload_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    label       TEXT,
    path        TEXT NOT NULL,
    sha256      TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    width       INTEGER NOT NULL,
    height      INTEGER NOT NULL,
    uploaded_by TEXT NOT NULL,
    created_at  REAL NOT NULL,
    job_id      TEXT
);
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    submitted_by  TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    status        TEXT NOT NULL,
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    outputs_at    REAL,
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
    bytes         INTEGER NOT NULL,
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
    lease_expires REAL,
    created_at    REAL NOT NULL,
    assigned_at   REAL,
    finished_at   REAL,
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
    reported_at REAL,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS units_by_status ON units (status, job_id, position);
CREATE INDEX IF NOT EXISTS runs_by_unit ON runs (unit_id, position);
CREATE INDEX IF NOT EXISTS runs_by_job ON runs (job_id, position);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, created_at);
"""


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


class Store:
    """One connection, one lock: every request runs its queries as one transaction."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.executescript(SCHEMA)
        self._lock = threading.RLock()

    @contextmanager
    def tx(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
            except BaseException:
                self._db.execute("ROLLBACK")
                raise
            self._db.execute("COMMIT")

    def close(self) -> None:
        self._db.close()

    # Devices -----------------------------------------------------------

    def create_device(self, name: str, now: float) -> str:
        """Revoke every existing key and return a new one (shown only once)."""
        key = f"pbd_{secrets.token_urlsafe(32)}"
        with self.tx() as db:
            db.execute("UPDATE devices SET revoked = 1")
            db.execute(
                "INSERT INTO devices (device_id, name, key_hash, created_at) VALUES (?, ?, ?, ?)",
                (new_id("d"), name, hash_key(key), now),
            )
        return key

    def has_active_device(self) -> bool:
        with self.tx() as db:
            return db.execute("SELECT 1 FROM devices WHERE revoked = 0").fetchone() is not None

    def device_for_key(self, key: str) -> sqlite3.Row | None:
        digest = hash_key(key)
        with self.tx() as db:
            rows = db.execute("SELECT * FROM devices WHERE revoked = 0").fetchall()
        return next((row for row in rows if hmac.compare_digest(row["key_hash"], digest)), None)
