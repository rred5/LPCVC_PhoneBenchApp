"""Shared test setup.

API tests run against SQLite. Set PHONEBENCH_TEST_DATABASE_URL to a Postgres
server you can throw away to run them against Postgres as well; each test
gets its own schema there.
"""

from dataclasses import dataclass
import io
import itertools
import os
import secrets

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from phonebench.api import create_app
from phonebench.config import Config, load_config
from phonebench.fake_worker import FakeWorker
from phonebench.store import PostgresStore, SQLiteStore

POSTGRES_URL = os.environ.get("PHONEBENCH_TEST_DATABASE_URL")
DATABASES = ["sqlite"] + (["postgres"] if POSTGRES_URL else [])


def _postgres_admin(sql: str) -> None:
    import psycopg

    with psycopg.connect(POSTGRES_URL, autocommit=True) as connection:
        connection.execute(sql)


class Clock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def image_bytes(width: int = 64, height: int = 48, color=(200, 30, 30), mode: str = "RGB", fmt: str = "PNG", **save) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), color).save(buffer, format=fmt, **save)
    return buffer.getvalue()


class FakeAuth:
    """Stands in for Supabase Auth: the token is the email."""

    def email_for(self, token: str) -> str:
        from phonebench.auth import AuthError

        if "@" not in token:
            raise AuthError("Your sign-in has expired; sign in again")
        return token


@dataclass
class Env:
    cfg: Config
    app: object
    client: TestClient
    clock: Clock
    key: str

    def worker(self, seed: int = 0) -> FakeWorker:
        return FakeWorker(self.client, self.key, seed=seed)

    def upload(self, files: dict[str, bytes], max_edge: int | None = None, headers: dict | None = None) -> dict:
        """The website's upload flow: reserve slots, send each file, complete it."""
        response = self.client.post(
            "/api/v1/uploads",
            json={"max_edge": max_edge, "files": [{"name": name, "bytes": len(data)} for name, data in files.items()]},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        accepted, rejected = [], list(body["rejected"])
        for slot in body["uploads"]:
            sent = self.client.put(
                slot["upload_url"], content=files[slot["name"]], headers={**(headers or {}), "Content-Type": slot["content_type"]}
            )
            assert sent.status_code == 200, sent.text
            done = self.client.post(f"/api/v1/uploads/{slot['upload_id']}/complete", headers=headers)
            if done.status_code == 200:
                accepted.append(done.json())
            else:
                rejected.append({"name": slot["name"], "reason": done.json()["error"]["message"]})
        return {"accepted": accepted, "rejected": rejected}

    def submit(self, run: dict | None = None, name: str = "test job", images: dict[str, bytes] | None = None, headers=None, **sections):
        if images is None:
            images = {"real/a.png": image_bytes(color=(10, 200, 10)), "tampered/b.png": image_bytes(color=(10, 10, 200))}
        max_edge = sections.get("preprocessing", {}).get("max_edge")
        accepted = self.upload(images, max_edge=max_edge, headers=headers)["accepted"]
        settings = {"run": {"repetitions": 2, "rest_seconds": 0, **(run or {}), "images": [i["upload_id"] for i in accepted]}, **sections}
        return self.client.post("/api/v1/jobs", json={"name": name, "settings": settings}, headers=headers)

    def job(self, job_id: str) -> dict:
        return self.client.get(f"/api/v1/jobs/{job_id}").json()


_counter = itertools.count()


@pytest.fixture(params=DATABASES)
def make_env(request, tmp_path):
    stores = []

    def make(limits: dict | None = None, queue: dict | None = None, auth=None, storage=None) -> Env:
        cfg = load_config(tmp_path / f"data{next(_counter)}", limits=limits, queue=queue, env={})
        if request.param == "postgres":
            from psycopg.conninfo import make_conninfo

            schema = f"t_{secrets.token_hex(6)}"
            _postgres_admin(f"CREATE SCHEMA {schema}")
            store = PostgresStore(make_conninfo(POSTGRES_URL, options=f"-c search_path={schema}"), max_connections=2)
            store.create_schema()
            store.schema = schema
        else:
            store = SQLiteStore(cfg.db_path)
        stores.append(store)
        clock = Clock()
        # `storage` may be a function of the config, since the config is made here.
        storage = storage(cfg) if callable(storage) else storage
        app = create_app(cfg, clock=clock, store=store, storage=storage, auth=auth)
        key = store.create_device("phone", clock())
        return Env(cfg, app, TestClient(app), clock, key)

    yield make
    for store in stores:
        store.close()
        if getattr(store, "schema", None):
            _postgres_admin(f"DROP SCHEMA {store.schema} CASCADE")


@pytest.fixture
def env(make_env) -> Env:
    return make_env()
