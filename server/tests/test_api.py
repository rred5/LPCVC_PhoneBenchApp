"""End-to-end tests through the HTTP API, with the fake phone."""

import csv
import io
import json

from tests.conftest import image_bytes

TWO_MODELS = {"models": ["Qwen3-VL-2B-Instruct-GGUF", "Qwen3-VL-4B-Instruct-GGUF"]}


def _report(worker, unit, run, index=0):
    return worker.client.post(
        f"/api/v1/device/runs/{run['run_id']}", headers=worker.headers, json=worker.result(unit, run, index == 0)
    )


def test_full_flow(env):
    upload = env.upload(
        {"set/real/a.png": image_bytes(color=(10, 200, 10)), "set/tampered/b.png": image_bytes(color=(10, 10, 200)), "set/notes.txt": b"hi"}
    )
    assert [item["label"] for item in upload["accepted"]] == ["real", "tampered"]
    assert [item["name"] for item in upload["rejected"]] == ["set/notes.txt"]

    settings = {"run": {**TWO_MODELS, "repetitions": 2, "images": [item["upload_id"] for item in upload["accepted"]]}}
    response = env.client.post("/api/v1/jobs", json={"name": "compare 2B vs 4B", "settings": settings})
    assert response.status_code == 201, response.text
    job = response.json()
    assert job["runs"]["total"] == 8 and job["estimated_seconds"] > 0

    assert env.worker().run_until_idle() == 2
    detail = env.job(job["job_id"])
    assert detail["status"] == "done" and detail["runs"]["completed"] == 8

    rows = list(csv.DictReader(io.StringIO(env.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/runs.csv").content.decode("utf-8-sig"))))
    assert len(rows) == 8 and {row["status"] for row in rows} == {"passed"}
    assert {row["label"] for row in rows} == {"real", "tampered"}
    assert rows[0]["reported_total_tokens"] == str(int(rows[0]["prompt_tokens"]) + int(rows[0]["generated_tokens"]))

    summary = env.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/summary.json").json()
    assert [(group["model"], group["attempted"], group["passed"]) for group in summary] == [
        ("Qwen3-VL-2B-Instruct-GGUF", 4, 4),
        ("Qwen3-VL-4B-Instruct-GGUF", 4, 4),
    ]
    accuracy = env.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/accuracy.json").json()
    assert set(accuracy["groups"][0]["by_label"]) == {"real", "tampered"}

    # Originals are deleted once prepared, prepared images once the job is done.
    assert not [path for path in env.cfg.files_dir.rglob("*") if path.is_file()]
    assert (env.cfg.jobs_dir / job["job_id"] / "outputs" / "runs.csv").is_file()


def test_uploader_tags_beat_folder_names(env):
    pictures = {name: image_bytes() for name in ("set/real/a.png", "set/real/b.png", "set/plain/c.png", "set/plain/d.png")}
    upload = env.upload(pictures, labels={"set/real/a.png": "tampered", "set/plain/c.png": "real"})
    # A tag wins over the folder name; untagged files keep the folder's label, or none.
    assert {item["name"]: item["label"] for item in upload["accepted"]} == {
        "set/real/a.png": "tampered",
        "set/real/b.png": "real",
        "set/plain/c.png": "real",
        "set/plain/d.png": None,
    }

    settings = {"run": {"images": [item["upload_id"] for item in upload["accepted"]]}}
    job = env.client.post("/api/v1/jobs", json={"name": "tags", "settings": settings}).json()
    env.worker().run_until_idle()
    rows = list(csv.DictReader(io.StringIO(env.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/runs.csv").content.decode("utf-8-sig"))))
    assert {(row["image"], row["label"]) for row in rows} == {
        ("set/real/a.png", "tampered"),
        ("set/real/b.png", "real"),
        ("set/plain/c.png", "real"),
        ("set/plain/d.png", ""),
    }


def test_unknown_tag_is_refused(env):
    response = env.client.post("/api/v1/uploads", json={"files": [{"name": "a.png", "bytes": 10, "label": "fake"}]})
    assert response.status_code == 422


def test_device_auth_and_version(env):
    worker = env.worker()
    assert env.client.post("/api/v1/device/poll", json={"state": "idle"}).status_code == 401
    bad = {**worker.headers, "Authorization": "Bearer nope"}
    assert env.client.post("/api/v1/device/poll", headers=bad, json={"state": "idle"}).json()["error"]["code"] == "unauthorized"
    old = {**worker.headers, "PhoneBench-App-Version": "0.0.9"}
    assert env.client.post("/api/v1/device/poll", headers=old, json={"state": "idle"}).status_code == 426
    assert env.client.post("/api/v1/device/poll", headers=worker.headers, json={"state": "sleeping"}).status_code == 422


def test_images_only_for_the_leased_unit(env):
    job = env.submit().json()
    worker = env.worker()
    with env.app.state.store.tx() as db:
        image_id = db.execute("SELECT image_id FROM job_images WHERE job_id = ?", (job["job_id"],)).fetchone()[0]
    assert env.client.get(f"/api/v1/device/images/{image_id}", headers=worker.headers).status_code == 404
    unit = worker.take_unit()
    worker.fetch_images(unit)  # checks SHA-256


def test_unit_shape(env):
    env.submit(run={"prompts": ["one", "two"]})
    unit = env.worker().take_unit()
    assert unit["model"] == "Qwen3-VL-2B-Instruct-GGUF" and unit["backend"] == "NPU"
    assert unit["mode"] == "warm" and unit["warmup"] is True
    assert len(unit["images"]) == 2 and len(unit["prompts"]) == 2
    # Image by image, then prompt, then repetition.
    order = [(r["image_id"], r["prompt_id"], r["repetition"]) for r in unit["runs"]]
    assert order == sorted(order, key=lambda r: ([i["image_id"] for i in unit["images"]].index(r[0]), [p["prompt_id"] for p in unit["prompts"]].index(r[1]), r[2]))


def test_lease_expiry_requeues_the_remaining_runs(env):
    env.submit()
    worker = env.worker()
    unit = worker.take_unit()
    assert len(unit["runs"]) == 4
    assert _report(worker, unit, unit["runs"][0]).status_code == 200

    env.clock.advance(env.cfg.queue["lease_seconds"] + 1)
    late = _report(worker, unit, unit["runs"][1])
    assert late.status_code == 409 and late.json()["error"]["code"] == "lease_lost"

    retry = worker.take_unit()
    assert retry["unit_id"] != unit["unit_id"]
    assert [run["run_id"] for run in retry["runs"]] == [run["run_id"] for run in unit["runs"][1:]]


def test_a_run_that_kills_the_worker_twice_is_skipped(env):
    job = env.submit().json()
    worker = env.worker()
    first = worker.take_unit()
    for _ in range(2):
        env.clock.advance(env.cfg.queue["lease_seconds"] + 1)
        unit = worker.take_unit()
    assert len(unit["runs"]) == 3  # the first run was skipped
    worker.run_unit(unit)
    detail = env.job(job["job_id"])
    assert detail["status"] == "done" and detail["runs"]["error"] == 1 and detail["runs"]["completed"] == 3
    runs = env.client.get(f"/api/v1/jobs/{job['job_id']}/runs").json()["runs"]
    failed = [run for run in runs if run["status"] == "failed"]
    assert failed[0]["run_id"] == first["runs"][0]["run_id"] and "2 times" in failed[0]["error"]


def test_restarted_app_gets_its_work_back(env):
    env.submit()
    worker = env.worker()
    unit = worker.take_unit()
    _report(worker, unit, unit["runs"][0])
    # The app restarted: it polls idle with no unit.
    again = worker.take_unit()
    assert again["unit_id"] != unit["unit_id"] and len(again["runs"]) == 3


def test_pause_releases_without_counting_a_failure(env):
    env.submit()
    worker = env.worker()
    assert worker.run_unit(worker.take_unit(), stop_after=1) == "released"
    resumed = worker.take_unit()
    assert len(resumed["runs"]) == 3
    with env.app.state.store.tx() as db:
        assert db.execute("SELECT attempts FROM units WHERE unit_id = ?", (resumed["unit_id"],)).fetchone()[0] == 0


def test_three_failures_give_up(env):
    job = env.submit().json()
    worker = env.worker()
    for attempt in range(3):
        unit = worker.take_unit()
        assert unit is not None, f"attempt {attempt}"
        worker.finish(unit, "failed", "model load failed")
    assert worker.take_unit() is None
    detail = env.job(job["job_id"])
    assert detail["status"] == "failed" and detail["runs"]["error"] == 4
    assert "failed 3 times: model load failed" in detail["error"]


def test_cancel_while_running(env):
    job = env.submit(run=TWO_MODELS)
    worker = env.worker()
    unit = worker.take_unit()
    _report(worker, unit, unit["runs"][0])
    assert env.client.post(f"/api/v1/jobs/{job.json()['job_id']}/cancel").json()["status"] == "cancelled"

    heartbeat = worker.poll("running", unit["unit_id"]).json()
    assert heartbeat["cancel_unit"] is True and heartbeat["unit"] is None
    assert _report(worker, unit, unit["runs"][1]).json() == {"continue": False}
    worker.finish(unit, "cancelled")

    detail = env.job(job.json()["job_id"])
    assert detail["status"] == "cancelled" and detail["finished_at"] is not None
    assert detail["runs"]["completed"] == 2 and detail["runs"]["cancelled"] == 6
    assert worker.take_unit() is None


def test_repeated_report_keeps_the_first(env):
    job = env.submit()
    worker = env.worker()
    unit = worker.take_unit()
    run = unit["runs"][0]
    first = worker.result(unit, run, True)
    second = {**worker.result(unit, run, False), "response": "different"}
    for body in (first, second):
        assert env.client.post(f"/api/v1/device/runs/{run['run_id']}", headers=worker.headers, json=body).status_code == 200
    runs = env.client.get(f"/api/v1/jobs/{job.json()['job_id']}/runs").json()["runs"]
    assert next(r for r in runs if r["run_id"] == run["run_id"])["response"] == first["response"]


def test_done_with_unreported_runs_requeues_them(env):
    env.submit()
    worker = env.worker()
    unit = worker.take_unit()
    worker.finish(unit, "done")
    assert len(worker.take_unit()["runs"]) == 4


def test_foreign_unit_is_rejected(env):
    env.submit()
    worker = env.worker()
    worker.take_unit()
    assert worker.poll("running", "u_someone_else").status_code == 409


def test_queue_pause(make_env):
    env = make_env(queue={"paused": True})
    env.submit()
    assert env.worker().take_unit() is None


def test_queued_job_limit(make_env):
    env = make_env(limits={"max_queued_jobs_per_user": 1})
    assert env.submit().status_code == 201
    second = env.submit()
    assert second.status_code == 429 and second.json()["error"]["code"] == "too_many_jobs"


def test_storage_budget(make_env):
    env = make_env(limits={"storage_budget_bytes": 10})
    rejected = env.upload({"a.png": image_bytes()})["rejected"]
    assert "storage is full" in rejected[0]["reason"]


def test_images_cannot_be_reused_or_invented(env):
    accepted = env.upload({"a.png": image_bytes()})["accepted"]
    settings = {"run": {"images": [accepted[0]["upload_id"]], "repetitions": 1}}
    assert env.client.post("/api/v1/jobs", json={"name": "one", "settings": settings}).status_code == 201
    again = env.client.post("/api/v1/jobs", json={"name": "two", "settings": settings})
    assert again.status_code == 422 and "already used" in again.json()["error"]["message"]
    made_up = env.client.post("/api/v1/jobs", json={"name": "three", "settings": {"run": {"images": ["up_nope"]}}})
    assert made_up.status_code == 422


def test_invalid_settings_message(env):
    response = env.submit(run={"models": ["Qwen2.5-VL-7B-Instruct"], "backends": ["CPU"]})
    assert response.status_code == 422 and "runs on NPU only" in response.json()["error"]["message"]


def test_device_status(env):
    worker = env.worker()
    worker.poll("idle")
    devices = env.client.get("/api/v1/device/status").json()["devices"]
    assert devices[0]["online"] and devices[0]["state"] == "idle"
    env.clock.advance(60)
    assert env.client.get("/api/v1/device/status").json()["devices"][0]["state"] == "offline"


def test_outputs_while_running(env):
    job = env.submit().json()
    worker = env.worker()
    unit = worker.take_unit()
    _report(worker, unit, unit["runs"][0])
    summary = env.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/summary.json").json()
    assert summary[0]["passed"] == 1
    job_json = json.loads(env.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/job.json").content)
    assert job_json["status"] == "running" and len(job_json["images"]) == 2


def test_multi_turn_prompt(env):
    upload = env.upload({"set/real/a.png": image_bytes(), "set/tampered/b.png": image_bytes(color=(10, 10, 200))})
    images = [item["upload_id"] for item in upload["accepted"]]
    turns = ["Real or tampered?", "Box it in this {width}x{height} image."]
    response = env.client.post("/api/v1/jobs", json={"name": "two stage", "settings": {"run": {"images": images, "prompts": ["plain", turns], "repetitions": 1}}})
    assert response.status_code == 201, response.text
    job_id = response.json()["job_id"]
    assert response.json()["runs"]["total"] == 4

    worker = env.worker()
    unit = worker.take_unit()
    # Every prompt lists its turns; text stays the first turn for older apps.
    assert [(prompt["text"], prompt["turns"]) for prompt in unit["prompts"]] == [("plain", ["plain"]), (turns[0], turns)]
    worker.run_unit(unit)

    job = env.client.get(f"/api/v1/jobs/{job_id}/outputs/job.json").json()
    assert job["prompts"] == ["plain", turns]
    read = lambda name: list(csv.DictReader(io.StringIO(env.client.get(f"/api/v1/jobs/{job_id}/outputs/{name}").content.decode("utf-8-sig"))))
    runs = read("runs.csv")
    assert sorted(row["turns"] for row in runs) == ["1", "1", "2", "2"]
    # runs.csv keeps the first turn, so verdict scoring still reads stage 1.
    multi = [row for row in runs if row["turns"] == "2"]
    assert all(row["binary_correct"] in ("True", "False") for row in multi)
    rows = read("turns.csv")
    assert [(row["image"], row["turn"]) for row in rows] == [(row["image"], str(t)) for row in multi for t in (1, 2)]
    assert rows[1]["response"] == '{"turn": 2, "boxes": []}' and rows[1]["ttft_ms"] == "150.0"


def test_single_turn_job_has_no_turns_csv(env):
    job = env.submit().json()
    env.worker().run_until_idle()
    assert env.client.get(f"/api/v1/jobs/{job['job_id']}/outputs/turns.csv").status_code == 404
