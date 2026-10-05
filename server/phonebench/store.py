"""The database: SQLite in local mode, Postgres (Supabase) in cloud mode.

Both run the same SQL. Queries use `?` placeholders; the Postgres store
rewrites them. The Postgres schema is the migration in supabase/migrations/;
SQLite's is below, and a test checks that they match.

Every write transaction (`tx`) is serialized (SQLite: one connection and a
lock; Postgres: a transaction-scoped advisory lock), so the queue logic never
races, even with several server instances. Reads that change nothing (`read`)
skip the lock in Postgres, so the website and key checks never hold up the
phone.
"""

from contextlib import contextmanager
import hashlib
import hmac
import secrets
import sqlite3
import threading
from pathlib import Path

SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    key_hash    TEXT NOT NULL,
    revoked     INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    last_seen   REAL,
    status_json TEXT
);
CREATE TABLE IF NOT EXISTS members (
    email      TEXT PRIMARY KEY,
    is_admin   INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (
    upload_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    label          TEXT,
    uploaded_by    TEXT NOT NULL,
    created_at     REAL NOT NULL,
    max_edge       INTEGER NOT NULL,
    declared_bytes INTEGER NOT NULL,
    status         TEXT NOT NULL,
    path           TEXT,
    sha256         TEXT,
    bytes          INTEGER,
    width          INTEGER,
    height         INTEGER,
    source_sha256  TEXT,
    source_bytes   INTEGER,
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
    deliveries    INTEGER NOT NULL DEFAULT 0,
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
CREATE INDEX IF NOT EXISTS uploads_by_status ON uploads (status, created_at);
"""
TABLES = ("devices", "members", "uploads", "jobs", "job_images", "prompts", "units", "runs")
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "supabase" / "migrations"

# Any constant works; every Postgres transaction takes this lock first.
ADVISORY_LOCK = 72_011_017


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    return email.strip().lower()


class Store:
    """Transactions, plus the operations that are not part of the queue."""

    def tx(self):
        """A write transaction: a context manager yielding an object with `execute(sql, params)`."""
        raise NotImplementedError

    def read(self):
        """A read-only transaction; it may see slightly older data than `tx`."""
        return self.tx()

    def close(self) -> None:
        pass

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
        with self.read() as db:
            return db.execute("SELECT 1 FROM devices WHERE revoked = 0").fetchone() is not None

    def device_for_key(self, key: str):
        digest = hash_key(key)
        with self.read() as db:
            rows = db.execute("SELECT * FROM devices WHERE revoked = 0").fetchall()
        return next((row for row in rows if hmac.compare_digest(row["key_hash"], digest)), None)

    # Members: who may sign in ------------------------------------------

    def member(self, email: str):
        with self.read() as db:
            return db.execute("SELECT * FROM members WHERE email = ?", (normalize_email(email),)).fetchone()

    def members(self) -> list:
        with self.read() as db:
            return db.execute("SELECT * FROM members ORDER BY email").fetchall()

    def add_member(self, email: str, is_admin: bool, now: float) -> None:
        with self.tx() as db:
            db.execute("DELETE FROM members WHERE email = ?", (normalize_email(email),))
            db.execute(
                "INSERT INTO members (email, is_admin, created_at) VALUES (?, ?, ?)",
                (normalize_email(email), int(is_admin), now),
            )

    def remove_member(self, email: str) -> bool:
        with self.tx() as db:
            return db.execute("DELETE FROM members WHERE email = ?", (normalize_email(email),)).rowcount > 0


class SQLiteStore(Store):
    """One connection, one lock: every request runs its queries as one transaction."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._migrate()
        self._db.executescript(SQLITE_SCHEMA)
        self._lock = threading.RLock()

    def _migrate(self) -> None:
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(uploads)")}
        if columns and "status" not in columns:
            # Uploads from before cloud mode; their files were already used and
            # deleted. Keep the rows under another name.
            self._db.execute("ALTER TABLE uploads RENAME TO uploads_v0")
        columns = {row["name"] for row in self._db.execute("PRAGMA table_info(units)")}
        if columns and "deliveries" not in columns:
            self._db.execute("ALTER TABLE units ADD COLUMN deliveries INTEGER NOT NULL DEFAULT 0")

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


class Row(dict):
    """A Postgres row that can also be read by position, like sqlite3.Row."""

    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _row_factory(cursor):
    names = [column.name for column in cursor.description or ()]
    return lambda values: Row(zip(names, values))


class _PostgresTx:
    """Runs the shared `?`-style SQL on a psycopg connection."""

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql: str, params=()):
        return self._connection.execute(sql.replace("?", "%s"), params)


class PostgresStore(Store):
    def __init__(self, url: str, max_connections: int = 4):
        from psycopg_pool import ConnectionPool

        self._pool = ConnectionPool(
            url,
            min_size=1,
            max_size=max_connections,
            # The Supabase pooler in transaction mode cannot keep prepared statements.
            kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": _row_factory},
            check=ConnectionPool.check_connection,
            max_idle=60,
            open=True,
        )

    def create_schema(self) -> None:
        """Apply supabase/migrations in order; each is safe to repeat."""
        with self._pool.connection() as connection, connection.transaction():
            for migration in sorted(MIGRATIONS_DIR.glob("*.sql")):
                connection.execute(migration.read_text(encoding="utf-8"))

    @contextmanager
    def tx(self):
        with self._pool.connection() as connection, connection.transaction():
            connection.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK,))
            yield _PostgresTx(connection)

    @contextmanager
    def read(self):
        with self._pool.connection() as connection, connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            yield _PostgresTx(connection)

    def close(self) -> None:
        self._pool.close()


def open_store(cfg) -> Store:
    if cfg.database_url:
        return PostgresStore(cfg.database_url)
    return SQLiteStore(cfg.db_path)
