from dataclasses import dataclass
import io
import itertools

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from phonebench.api import create_app
from phonebench.config import Config, load_config
from phonebench.fake_worker import FakeWorker


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


@dataclass
class Env:
    cfg: Config
    app: object
    client: TestClient
    clock: Clock
    key: str

    def worker(self, seed: int = 0) -> FakeWorker:
        return FakeWorker(self.client, self.key, seed=seed)

    def upload(self, files: dict[str, bytes]) -> dict:
        response = self.client.post(
            "/api/v1/uploads",
            files=[("files", (name.rsplit("/", 1)[-1], data)) for name, data in files.items()],
            data={"paths": list(files)},
        )
        assert response.status_code == 200, response.text
        return response.json()

    def submit(self, run: dict | None = None, name: str = "test job", images: dict[str, bytes] | None = None, **sections):
        if images is None:
            images = {"real/a.png": image_bytes(color=(10, 200, 10)), "tampered/b.png": image_bytes(color=(10, 10, 200))}
        accepted = self.upload(images)["accepted"]
        settings = {"run": {"repetitions": 2, "rest_seconds": 0, **(run or {}), "images": [i["upload_id"] for i in accepted]}, **sections}
        return self.client.post("/api/v1/jobs", json={"name": name, "settings": settings})

    def job(self, job_id: str) -> dict:
        return self.client.get(f"/api/v1/jobs/{job_id}").json()


_counter = itertools.count()


@pytest.fixture
def make_env(tmp_path):
    def make(limits: dict | None = None, queue: dict | None = None) -> Env:
        cfg = load_config(tmp_path / f"data{next(_counter)}", limits=limits, queue=queue)
        clock = Clock()
        app = create_app(cfg, clock=clock)
        key = app.state.store.create_device("phone", clock())
        return Env(cfg, app, TestClient(app), clock, key)

    return make


@pytest.fixture
def env(make_env) -> Env:
    return make_env()
