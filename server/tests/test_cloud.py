"""Uploads, sign-in and the cloud-only behavior, without a real Supabase project."""

import io
import json
import sqlite3
import zipfile

import httpx
import pytest

from phonebench import api as api_module
from phonebench import images
from phonebench.auth import AuthError, SupabaseAuth
from phonebench.config import load_config, read_env_file
from phonebench.storage import LocalStorage, SupabaseStorage
from phonebench.store import TABLES, SQLiteStore
from tests.conftest import POSTGRES_URL, FakeAuth, image_bytes

ALICE = {"Authorization": "Bearer alice@example.com"}
BOB = {"Authorization": "Bearer bob@example.com"}
ADMIN = {"Authorization": "Bearer admin@example.com"}


def _slots(env, files: dict, max_edge=None, headers=None):
    response = env.client.post(
        "/api/v1/uploads",
        json={"max_edge": max_edge, "files": [{"name": name, "bytes": size} for name, size in files.items()]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    return response.json()


# Uploads -------------------------------------------------------------------


def test_complete_before_sending_the_file(env):
    slot = _slots(env, {"a.png": 100})["uploads"][0]
    response = env.client.post(f"/api/v1/uploads/{slot['upload_id']}/complete")
    assert response.status_code == 409 and response.json()["error"]["code"] == "not_uploaded"


def test_a_fake_image_is_rejected_and_forgotten(env):
    result = env.upload({"real/a.png": b"not an image at all"})
    assert result["accepted"] == [] and "not a readable image" in result["rejected"][0]["reason"]
    with env.app.state.store.tx() as db:
        assert db.execute("SELECT COUNT(*) FROM uploads").fetchone()[0] == 0
    assert not [path for path in env.cfg.files_dir.rglob("*") if path.is_file()]


def test_completing_twice_is_harmless(env):
    upload = env.upload({"a.png": image_bytes()})["accepted"][0]
    again = env.client.post(f"/api/v1/uploads/{upload['upload_id']}/complete")
    assert again.status_code == 200 and again.json() == upload


def test_slot_checks(env):
    body = _slots(env, {"notes.txt": 10, "big.png": 10**9, "../escape.png": 10, "ok.JPG": 10})
    reasons = {item["name"]: item["reason"] for item in body["rejected"]}
    assert "only PNG, JPEG and WebP" in reasons["notes.txt"]
    assert "larger than" in reasons["big.png"]
    assert "plain relative path" in reasons["../escape.png"]
    assert [slot["name"] for slot in body["uploads"]] == ["ok.JPG"]
    assert body["uploads"][0]["content_type"] == "image/jpeg"
    too_small = env.client.post("/api/v1/uploads", json={"max_edge": 10, "files": [{"name": "a.png", "bytes": 1}]})
    assert too_small.status_code == 422


def test_images_are_prepared_at_upload(env):
    original = image_bytes(1000, 500, color=(90, 120, 150))
    upload = env.upload({"real/wide.png": original}, max_edge=256)["accepted"][0]
    expected, meta = images.prepare(original, 256)
    assert (upload["width"], upload["height"]) == (256, 128) and upload["bytes"] == len(expected)
    assert (upload["source_width"], upload["source_height"]) == (1000, 500)
    assert env.app.state.storage.get(f"images/{upload['upload_id']}.png", 10**7) == expected
    # The original is gone once prepared.
    assert env.app.state.storage.get(f"incoming/{upload['upload_id']}", 10**7) is None


def test_job_max_edge_must_match_the_upload(env):
    upload = env.upload({"a.png": image_bytes()}, max_edge=512)["accepted"][0]
    settings = {"run": {"images": [upload["upload_id"]], "repetitions": 1}, "preprocessing": {"max_edge": 768}}
    response = env.client.post("/api/v1/jobs", json={"name": "j", "settings": settings})
    assert response.status_code == 422 and "different max_edge" in response.json()["error"]["message"]
    settings["preprocessing"]["max_edge"] = 512
    assert env.client.post("/api/v1/jobs", json={"name": "j", "settings": settings}).status_code == 201


def test_unfinished_uploads_cannot_be_used(env):
    slot = _slots(env, {"a.png": 100})["uploads"][0]
    settings = {"run": {"images": [slot["upload_id"]], "repetitions": 1}}
    response = env.client.post("/api/v1/jobs", json={"name": "j", "settings": settings})
    assert response.status_code == 422 and "unfinished" in response.json()["error"]["message"]


def test_abandoned_uploads_are_cleared_after_a_day(env):
    upload = env.upload({"a.png": image_bytes()})["accepted"][0]
    slot = _slots(env, {"b.png": 100})["uploads"][0]
    env.clock.advance(25 * 3600)
    _slots(env, {"c.png": 100})
    for upload_id in (upload["upload_id"], slot["upload_id"]):
        assert env.client.post(f"/api/v1/uploads/{upload_id}/complete").status_code == 404
    assert env.app.state.storage.get(f"images/{upload['upload_id']}.png", 10**7) is None


def test_sending_too_much_data(make_env):
    env = make_env(limits={"max_upload_bytes": 1000})
    slot = _slots(env, {"a.png": 1000})["uploads"][0]
    response = env.client.put(slot["upload_url"], content=b"x" * 1001)
    assert response.status_code == 413


# Sign-in and members --------------------------------------------------------


@pytest.fixture
def team(make_env):
    env = make_env(auth=FakeAuth())
    store = env.app.state.store
    store.add_member("Alice@Example.com", False, 0)
    store.add_member("bob@example.com", False, 0)
    store.add_member("admin@example.com", True, 0)
    return env


def test_sign_in_is_required(team):
    assert team.client.get("/api/v1/jobs").status_code == 401
    assert team.client.get("/api/v1/jobs", headers={"Authorization": "Bearer junk"}).status_code == 401
    stranger = team.client.get("/api/v1/jobs", headers={"Authorization": "Bearer eve@example.com"})
    assert stranger.status_code == 403 and stranger.json()["error"]["code"] == "not_a_member"
    me = team.client.get("/api/v1/me", headers=ALICE).json()
    assert me == {"email": "alice@example.com", "is_admin": False, "cloud": False}
    assert team.client.get("/api/v1/health").status_code == 200


def test_only_the_submitter_or_an_admin_can_cancel(team):
    first = team.submit(headers=ALICE, run={"repetitions": 1}).json()
    second = team.submit(headers=ALICE, run={"repetitions": 1}).json()
    assert first["submitted_by"] == "alice@example.com"
    refused = team.client.post(f"/api/v1/jobs/{first['job_id']}/cancel", headers=BOB)
    assert refused.status_code == 403 and refused.json()["error"]["code"] == "not_yours"
    assert team.client.post(f"/api/v1/jobs/{first['job_id']}/cancel", headers=ALICE).json()["status"] == "cancelled"
    assert team.client.post(f"/api/v1/jobs/{second['job_id']}/cancel", headers=ADMIN).json()["status"] == "cancelled"


def test_uploads_belong_to_their_uploader(team):
    upload = team.upload({"a.png": image_bytes()}, headers=ALICE)["accepted"][0]
    assert team.client.post(f"/api/v1/uploads/{upload['upload_id']}/complete", headers=BOB).status_code == 404
    settings = {"run": {"images": [upload["upload_id"]], "repetitions": 1}}
    stolen = team.client.post("/api/v1/jobs", json={"name": "mine now", "settings": settings}, headers=BOB)
    assert stolen.status_code == 422
    slot = _slots(team, {"b.png": 100}, headers=ALICE)["uploads"][0]
    assert team.client.put(slot["upload_url"], content=b"x", headers=BOB).status_code == 404


def test_the_phone_does_not_need_a_sign_in(team):
    team.submit(headers=ALICE)
    assert team.worker().run_until_idle() == 1


def test_options_for_the_job_form(team):
    options = team.client.get("/api/v1/options", headers=ALICE).json()
    assert options["defaults"]["run"]["repetitions"] == 10
    assert "storage_budget_bytes" not in options["limits"]
    assert {model["id"] for model in options["models"]} >= {"Qwen3-VL-2B-Instruct-GGUF"}


def test_members(env):
    store = env.app.state.store
    store.add_member(" Carol@Example.COM ", True, 0)
    store.add_member("carol@example.com", False, 0)
    assert [(m["email"], m["is_admin"]) for m in store.members()] == [("carol@example.com", 0)]
    assert store.member("CAROL@example.com") is not None
    assert store.remove_member("carol@example.com") and not store.remove_member("carol@example.com")


# Cloud behavior, with a stand-in for the bucket ----------------------------


class FakeBucket(LocalStorage):
    cloud = True

    def upload_url(self, key: str) -> str:
        return f"https://bucket.test/upload/{key}"

    def download_url(self, key: str, filename: str | None = None) -> str:
        return f"https://bucket.test/download/{key}" + (f"?download={filename}" if filename else "")


@pytest.fixture
def cloud(make_env):
    return make_env(storage=lambda cfg: FakeBucket(cfg.files_dir))


def _cloud_upload(env, name: str, data: bytes) -> dict:
    slot = _slots(env, {name: len(data)})["uploads"][0]
    assert slot["upload_url"] == f"https://bucket.test/upload/incoming/{slot['upload_id']}"
    # The browser sends the file straight to the bucket.
    env.app.state.storage.put(f"incoming/{slot['upload_id']}", data, slot["content_type"])
    response = env.client.post(f"/api/v1/uploads/{slot['upload_id']}/complete")
    assert response.status_code == 200, response.text
    return response.json()


def test_cloud_phone_gets_images_from_the_bucket(cloud):
    upload = _cloud_upload(cloud, "real/a.png", image_bytes())
    settings = {"run": {"images": [upload["upload_id"]], "repetitions": 1}}
    assert cloud.client.post("/api/v1/jobs", json={"name": "j", "settings": settings}).status_code == 201
    worker = cloud.worker()
    unit = worker.take_unit()
    response = cloud.client.get(unit["images"][0]["url"], headers=worker.headers, follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == f"https://bucket.test/download/images/{upload['upload_id']}.png"


def test_cloud_has_no_direct_upload_path(cloud):
    slot = _slots(cloud, {"a.png": 10})["uploads"][0]
    assert cloud.client.put(f"/api/v1/uploads/{slot['upload_id']}/data", content=b"x").status_code == 404


def test_cloud_sends_large_outputs_through_the_bucket(cloud, monkeypatch):
    upload = _cloud_upload(cloud, "a.png", image_bytes())
    settings = {"run": {"images": [upload["upload_id"]], "repetitions": 1}}
    job = cloud.client.post("/api/v1/jobs", json={"name": "j", "settings": settings}).json()
    assert cloud.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/job.json").status_code == 200
    monkeypatch.setattr(api_module, "INLINE_BYTES", 10)
    big = cloud.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/job.json", follow_redirects=False)
    assert big.status_code == 307
    assert big.headers["location"].endswith(f"outputs/{job['job_id']}/job.json?download={job['job_id']}-job.json")
    # Cloud mode writes no output files to disk.
    assert not cloud.cfg.jobs_dir.exists()


# Outputs --------------------------------------------------------------------


def test_outputs_zip(env):
    job = env.submit().json()
    env.worker().run_until_idle()
    response = env.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/outputs.zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(response.content)).namelist()
    prefix = job["job_id"] + "/"
    assert {prefix + name for name in ("job.json", "runs.csv", "summary.json", "accuracy.json")} <= set(names)
    assert sum(name.startswith(prefix + "runs/") for name in names) == 4


# Config and storage plumbing ---------------------------------------------


def test_old_local_database_is_migrated(tmp_path):
    path = tmp_path / "old.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE uploads (upload_id TEXT PRIMARY KEY, name TEXT, path TEXT)")
        db.execute("INSERT INTO uploads VALUES ('up_1', 'L106.png', '/gone')")
    store = SQLiteStore(path)
    with store.tx() as db:
        assert db.execute("SELECT name FROM uploads_v0").fetchone()[0] == "L106.png"
        assert "status" in {row["name"] for row in db.execute("PRAGMA table_info(uploads)")}
    store.close()


def test_cloud_config_needs_every_setting(tmp_path):
    with pytest.raises(ValueError):
        load_config(env={"SUPABASE_URL": "https://x.supabase.co"})
    with pytest.raises(ValueError):
        load_config(env={"SUPABASE_URL": "http://x.supabase.co", "SUPABASE_SECRET_KEY": "k", "PHONEBENCH_DATABASE_URL": "u"})
    cfg = load_config(env={"SUPABASE_URL": "https://x.supabase.co/", "SUPABASE_SECRET_KEY": "k", "PHONEBENCH_DATABASE_URL": "u"})
    assert cfg.cloud and cfg.supabase_url == "https://x.supabase.co" and cfg.bucket == "phonebench"
    assert not load_config(env={}).cloud
    env_file = tmp_path / "cloud.env"
    env_file.write_text("# comment\nSUPABASE_URL = 'https://x.supabase.co'\n\nEMPTY=\n", encoding="utf-8")
    assert read_env_file(env_file) == {"SUPABASE_URL": "https://x.supabase.co", "EMPTY": ""}


def _mock(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_supabase_auth():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        token = request.headers["authorization"].removeprefix("Bearer ")
        if token == "expired":
            return httpx.Response(401, json={"msg": "expired"})
        confirmed = None if token == "unconfirmed" else "2026-10-01T00:00:00Z"
        providers = ["email"] if token == "password" else ["google"]
        return httpx.Response(
            200, json={"email": "Alice@Example.com", "email_confirmed_at": confirmed, "app_metadata": {"providers": providers}}
        )

    auth = SupabaseAuth("https://x.supabase.co", "sb_secret_k", client=_mock(handler))
    assert auth.email_for("good") == "Alice@Example.com"
    assert auth.email_for("good") == "Alice@Example.com"
    assert len(calls) == 1  # cached
    assert str(calls[0].url) == "https://x.supabase.co/auth/v1/user" and calls[0].headers["apikey"] == "sb_secret_k"
    for token in ("expired", "unconfirmed"):
        with pytest.raises(AuthError):
            auth.email_for(token)
    # A confirmed address is not enough: it must have signed in with Google.
    with pytest.raises(AuthError, match="Sign in with Google"):
        auth.email_for("password")


def test_supabase_storage_requests():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        if path.startswith("/storage/v1/object/upload/sign/"):
            return httpx.Response(200, json={"url": "/object/upload/sign/phonebench/incoming/up_1?token=t"})
        if path.startswith("/storage/v1/object/sign/"):
            assert json.loads(request.content) == {"expiresIn": 300}
            return httpx.Response(200, json={"signedURL": "/object/sign/phonebench/images/up_1.png?token=s"})
        if request.method == "GET" and path.endswith("/missing"):
            return httpx.Response(400, json={"error": "not_found"})
        if request.method == "GET":
            return httpx.Response(200, content=b"png bytes")
        return httpx.Response(200, json={})

    storage = SupabaseStorage("https://x.supabase.co", "sb_secret_k", "phonebench", client=_mock(handler))
    assert storage.upload_url("incoming/up_1") == "https://x.supabase.co/storage/v1/object/upload/sign/phonebench/incoming/up_1?token=t"
    assert storage.download_url("images/up_1.png", "a b.png").endswith("?token=s&download=a%20b.png")
    assert storage.get("images/up_1.png", 100) == b"png bytes"
    assert storage.get("incoming/missing", 100) is None
    storage.put("images/up_1.png", b"data", "image/png")
    put = seen[-1]
    assert put.method == "POST" and put.headers["x-upsert"] == "true" and put.headers["content-type"] == "image/png"
    storage.delete(["a", "b"])
    assert json.loads(seen[-1].content) == {"prefixes": ["a", "b"]}


def test_supabase_key_headers():
    new_style = SupabaseStorage("https://x.supabase.co", "sb_secret_k", "b")
    assert new_style.client.headers["apikey"] == "sb_secret_k" and "authorization" not in new_style.client.headers
    legacy = SupabaseStorage("https://x.supabase.co", "eyJhbGciOi.x.y", "b")
    assert legacy.client.headers["authorization"] == "Bearer eyJhbGciOi.x.y"


@pytest.mark.skipif(not POSTGRES_URL, reason="needs PHONEBENCH_TEST_DATABASE_URL")
def test_sqlite_and_postgres_schemas_match(tmp_path, make_env):
    env = make_env()
    if isinstance(env.app.state.store, SQLiteStore):
        pytest.skip("runs once, with the Postgres store")
    sqlite_store = SQLiteStore(tmp_path / "schema.sqlite3")
    with sqlite_store.tx() as db:
        sqlite_columns = {
            table: {row["name"] for row in db.execute(f"PRAGMA table_info({table})")} for table in TABLES
        }
    sqlite_store.close()
    with env.app.state.store.tx() as db:
        rows = db.execute(
            "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema = current_schema()"
        ).fetchall()
        locked = {row["relname"] for row in db.execute("SELECT relname FROM pg_class WHERE relrowsecurity")}
    postgres_columns = {}
    for row in rows:
        postgres_columns.setdefault(row["table_name"], set()).add(row["column_name"])
    assert postgres_columns == sqlite_columns
    assert set(TABLES) <= locked


# Phone failure modes ---------------------------------------------------------


def _crashes(env, run_id: str) -> int:
    with env.app.state.store.read() as db:
        return db.execute("SELECT crashes FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]


def test_a_lost_assignment_is_sent_again_not_counted_as_a_crash(env):
    env.submit()
    worker = env.worker()
    first = worker.take_unit()
    # The reply was lost: the phone never saw the unit and polls idle again.
    assert worker.take_unit()["unit_id"] == first["unit_id"]
    assert worker.take_unit()["unit_id"] == first["unit_id"]
    assert _crashes(env, first["runs"][0]["run_id"]) == 0
    # After three deliveries with nothing reported, it counts as a crash after all,
    # so a unit that kills the app before its first report cannot loop forever.
    fourth = worker.take_unit()
    assert fourth["unit_id"] != first["unit_id"]
    assert _crashes(env, first["runs"][0]["run_id"]) == 1
    assert worker.run_unit(fourth) == "done"


def test_a_resent_unit_still_completes_normally(env):
    job = env.submit().json()
    worker = env.worker()
    worker.take_unit()
    unit = worker.take_unit()
    assert worker.run_unit(unit) == "done"
    assert env.job(job["job_id"])["status"] == "done"


class FlakyDeletes(LocalStorage):
    """Storage whose deletes fail while `broken` is set."""

    broken = False

    def delete(self, keys):
        if self.broken:
            from phonebench.storage import UpstreamError

            raise UpstreamError("storage is down")
        super().delete(keys)


def test_cleanup_failures_never_fail_phone_requests(make_env):
    env = make_env(storage=lambda cfg: FlakyDeletes(cfg.files_dir))
    storage = env.app.state.storage
    job = env.submit().json()
    worker = env.worker()
    storage.broken = True
    # Finishing the unit finishes the job; deleting its images fails, quietly.
    assert worker.run_until_idle() == 1
    assert worker.poll().status_code == 200
    assert env.job(job["job_id"])["status"] == "done"
    assert [path for path in env.cfg.files_dir.rglob("*.png")]
    storage.broken = False
    assert worker.poll().status_code == 200
    assert not [path for path in env.cfg.files_dir.rglob("*.png")]
    with env.app.state.store.read() as db:
        assert db.execute("SELECT outputs_at FROM jobs WHERE job_id = ?", (job["job_id"],)).fetchone()[0] is not None


# Storage limits ----------------------------------------------------------------


def test_unfinished_uploads_are_capped_per_person(make_env):
    env = make_env(limits={"max_unfinished_uploads": 3})
    slots = _slots(env, {"a.png": 10, "b.png": 10, "c.png": 10})["uploads"]
    too_many = env.client.post("/api/v1/uploads", json={"files": [{"name": "d.png", "bytes": 10}]})
    assert too_many.status_code == 429 and too_many.json()["error"]["code"] == "too_many_uploads"
    assert env.client.delete(f"/api/v1/uploads/{slots[0]['upload_id']}").status_code == 200
    assert len(_slots(env, {"d.png": 10})["uploads"]) == 1


def test_files_cannot_exceed_their_declared_size(env, cloud):
    # Local: the upload endpoint stops reading.
    slot = _slots(env, {"a.png": 100})["uploads"][0]
    assert env.client.put(slot["upload_url"], content=image_bytes()).status_code == 413
    # Cloud: the file reached the bucket directly; completing it fails and removes it.
    data = image_bytes()
    slot = _slots(cloud, {"b.png": len(data) - 1})["uploads"][0]
    cloud.app.state.storage.put(f"incoming/{slot['upload_id']}", data, "image/png")
    response = cloud.client.post(f"/api/v1/uploads/{slot['upload_id']}/complete")
    assert response.status_code == 422 and "declared size" in response.json()["error"]["message"]
    assert cloud.app.state.storage.get(f"incoming/{slot['upload_id']}", 10**7) is None


def test_phone_polls_clear_abandoned_slots_after_three_hours(env):
    ready = env.upload({"a.png": image_bytes()})["accepted"][0]
    slot = _slots(env, {"b.png": 100})["uploads"][0]
    env.app.state.storage.put(f"incoming/{slot['upload_id']}", b"partial", "image/png")
    env.clock.advance(3.5 * 3600)
    env.worker().poll()
    assert env.client.post(f"/api/v1/uploads/{slot['upload_id']}/complete").status_code == 404
    assert env.app.state.storage.get(f"incoming/{slot['upload_id']}", 10**7) is None
    # A finished upload waits a day for its job.
    assert env.client.post(f"/api/v1/uploads/{ready['upload_id']}/complete").status_code == 200


def test_delete_upload(team):
    upload = team.upload({"a.png": image_bytes()}, headers=ALICE)["accepted"][0]
    assert team.client.delete(f"/api/v1/uploads/{upload['upload_id']}", headers=BOB).status_code == 404
    used = team.upload({"b.png": image_bytes()}, headers=ALICE)["accepted"][0]
    settings = {"run": {"images": [used["upload_id"]], "repetitions": 1}}
    assert team.client.post("/api/v1/jobs", json={"name": "j", "settings": settings}, headers=ALICE).status_code == 201
    assert team.client.delete(f"/api/v1/uploads/{used['upload_id']}", headers=ALICE).status_code == 409
    assert team.client.delete(f"/api/v1/uploads/{upload['upload_id']}", headers=ALICE).status_code == 200
    assert team.app.state.storage.get(f"images/{upload['upload_id']}.png", 10**7) is None


# Latency -----------------------------------------------------------------------


def test_job_list_counts_every_job(env):
    first = env.submit(run={"repetitions": 1}).json()
    env.clock.advance(1)  # the oldest job runs first
    second = env.submit(run={"repetitions": 3}).json()
    worker = env.worker()
    worker.run_unit(worker.take_unit())
    jobs = {job["job_id"]: job["runs"] for job in env.client.get("/api/v1/jobs").json()["jobs"]}
    assert jobs[first["job_id"]] == {"total": 2, "pending": 0, "completed": 2, "error": 0, "cancelled": 0}
    assert jobs[second["job_id"]] == {"total": 6, "pending": 6, "completed": 0, "error": 0, "cancelled": 0}


def test_postgres_reads_cannot_write(make_env):
    env = make_env()
    if isinstance(env.app.state.store, SQLiteStore):
        pytest.skip("Postgres only")
    import psycopg

    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        with env.app.state.store.read() as db:
            db.execute("DELETE FROM members")
