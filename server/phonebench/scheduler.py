"""The job queue: split jobs into units, hand units to the phone, recover from failures.

Every function takes an open transaction (`db`) and the current time, so the
API decides transaction boundaries and tests control the clock. The rules are
the ones in docs/API.md ("Leases and recovery").
"""

from datetime import datetime
from itertools import product
import json
import statistics

from .config import Config
from .store import new_id

TERMINAL_JOB = ("done", "failed", "cancelled")


class LeaseLost(Exception):
    """The unit is not, or no longer, assigned to this device."""


class NotFound(Exception):
    """No such job, unit, run or image."""


# Jobs ------------------------------------------------------------------


def insert_job(db, job_id: str, name: str, submitted_by: str, settings: dict, images: list[dict], now: float) -> None:
    """Store a job and split it into units and runs.

    `images` are the prepared images in job order, each a dict with the
    `job_images` columns except `job_id` and `position`.
    """
    db.execute(
        "INSERT INTO jobs (job_id, name, submitted_by, settings_json, status, created_at) VALUES (?, ?, ?, ?, 'queued', ?)",
        (job_id, name, submitted_by, json.dumps(settings), now),
    )
    for position, image in enumerate(images):
        db.execute(
            "INSERT INTO job_images (image_id, job_id, position, name, label, path, sha256, bytes, width, height, source_sha256)"
            " VALUES (:image_id, :job_id, :position, :name, :label, :path, :sha256, :bytes, :width, :height, :source_sha256)",
            {**image, "job_id": job_id, "position": position},
        )
    prompt_ids = []
    for position, text in enumerate(settings["run"]["prompts"]):
        prompt_ids.append(new_id("p"))
        db.execute(
            "INSERT INTO prompts (prompt_id, job_id, position, text) VALUES (?, ?, ?, ?)",
            (prompt_ids[-1], job_id, position, text),
        )
    run = settings["run"]
    # One unit per model and compute unit, in the order the job lists them;
    # within a unit, runs go image by image, then prompt, then repetition.
    for unit_position, (model, backend) in enumerate(product(run["models"], run["backends"])):
        unit_id = new_id("u")
        db.execute(
            "INSERT INTO units (unit_id, job_id, model, backend, position, status, created_at) VALUES (?, ?, ?, ?, ?, 'queued', ?)",
            (unit_id, job_id, model, backend, unit_position, now),
        )
        position = 0
        for image in images:
            for prompt_id in prompt_ids:
                for repetition in range(1, run["repetitions"] + 1):
                    db.execute(
                        "INSERT INTO runs (run_id, job_id, unit_id, model, backend, image_id, prompt_id, repetition, position, status)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')",
                        (new_id("r"), job_id, unit_id, model, backend, image["image_id"], prompt_id, repetition, position),
                    )
                    position += 1


def get_job(db, job_id: str):
    job = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    if job is None:
        raise NotFound(job_id)
    return job


def active_jobs_for(db, submitted_by: str) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM jobs WHERE submitted_by = ? AND status IN ('queued', 'running')", (submitted_by,)
    ).fetchone()[0]


def cancel_job(db, cfg: Config, job_id: str, now: float) -> None:
    """Cancel a job. A unit the phone is running stops when the phone next polls."""
    job = get_job(db, job_id)
    if job["status"] in TERMINAL_JOB:
        return
    db.execute("UPDATE jobs SET status = 'cancelled' WHERE job_id = ?", (job_id,))
    queued = [row["unit_id"] for row in db.execute("SELECT unit_id FROM units WHERE job_id = ? AND status = 'queued'", (job_id,))]
    for unit_id in queued:
        db.execute("UPDATE units SET status = 'cancelled', finished_at = ? WHERE unit_id = ?", (now, unit_id))
        db.execute("UPDATE runs SET status = 'cancelled' WHERE unit_id = ? AND status = 'pending'", (unit_id,))
    refresh_job(db, job_id, now)


def refresh_job(db, job_id: str, now: float) -> None:
    """Mark a job finished once no unit of it is queued or running."""
    job = get_job(db, job_id)
    if job["finished_at"] is not None:
        return
    busy = db.execute(
        "SELECT COUNT(*) FROM units WHERE job_id = ? AND status IN ('queued', 'assigned')", (job_id,)
    ).fetchone()[0]
    if busy:
        return
    status = job["status"] if job["status"] == "cancelled" else ("failed" if job["error"] else "done")
    db.execute("UPDATE jobs SET status = ?, finished_at = ? WHERE job_id = ?", (status, now, job_id))


def jobs_needing_outputs(db) -> list[str]:
    return [row["job_id"] for row in db.execute("SELECT job_id FROM jobs WHERE finished_at IS NOT NULL AND outputs_at IS NULL")]


def estimate_seconds(db, cfg: Config, settings: dict) -> float:
    """Rough job duration from past runs of the same model and compute unit."""
    durations: dict[tuple[str, str], list[float]] = {}
    for row in db.execute(
        "SELECT model, backend, result_json FROM runs WHERE status = 'completed' ORDER BY reported_at DESC LIMIT 2000"
    ):
        result = json.loads(row["result_json"])
        try:
            started = datetime.fromisoformat(result["started_at"])
            finished = datetime.fromisoformat(result["finished_at"])
        except (KeyError, TypeError, ValueError):
            continue
        durations.setdefault((row["model"], row["backend"]), []).append((finished - started).total_seconds())
    run = settings["run"]
    per_unit_runs = len(run["images"]) * len(run["prompts"]) * run["repetitions"]
    total = 0.0
    for model, backend in product(run["models"], run["backends"]):
        seen = durations.get((model, backend))
        run_s = statistics.mean(seen) if seen else cfg.queue["default_run_seconds"]
        # Warm mode adds one warm-up run per unit; cold mode reloads each run.
        runs = per_unit_runs + 1 if run["mode"] == "warm" else per_unit_runs * 1.2
        total += runs * run_s + max(0, per_unit_runs - 1) * run["rest_seconds"]
    return total


# Units -------------------------------------------------------------------


def _requeue(db, unit, now: float, attempts: int) -> str | None:
    """Move a unit's pending runs into a new queued unit; return its id, or None if nothing is left."""
    if get_job(db, unit["job_id"])["status"] == "cancelled":
        db.execute("UPDATE runs SET status = 'cancelled' WHERE unit_id = ? AND status = 'pending'", (unit["unit_id"],))
        return None
    pending = db.execute(
        "SELECT COUNT(*) FROM runs WHERE unit_id = ? AND status = 'pending'", (unit["unit_id"],)
    ).fetchone()[0]
    if not pending:
        return None
    new_unit = new_id("u")
    db.execute(
        "INSERT INTO units (unit_id, job_id, model, backend, position, status, attempts, created_at)"
        " VALUES (?, ?, ?, ?, ?, 'queued', ?, ?)",
        (new_unit, unit["job_id"], unit["model"], unit["backend"], unit["position"], attempts, now),
    )
    db.execute("UPDATE runs SET unit_id = ? WHERE unit_id = ? AND status = 'pending'", (new_unit, unit["unit_id"]))
    return new_unit


def _recover_lost(db, cfg: Config, unit, now: float, reason: str) -> None:
    """The phone stopped working on `unit` partway: blame the run in progress, requeue the rest."""
    in_progress = db.execute(
        "SELECT * FROM runs WHERE unit_id = ? AND status = 'pending' ORDER BY position LIMIT 1", (unit["unit_id"],)
    ).fetchone()
    if in_progress is not None:
        crashes = in_progress["crashes"] + 1
        if crashes >= cfg.queue["max_run_crashes"]:
            db.execute(
                "UPDATE runs SET crashes = ?, status = 'error', error = ?, reported_at = ? WHERE run_id = ?",
                (crashes, f"Worker stopped during this run {crashes} times", now, in_progress["run_id"]),
            )
        else:
            db.execute("UPDATE runs SET crashes = ? WHERE run_id = ?", (crashes, in_progress["run_id"]))
    db.execute(
        "UPDATE units SET status = 'expired', finished_at = ?, error = ? WHERE unit_id = ?", (now, reason, unit["unit_id"])
    )
    _requeue(db, unit, now, unit["attempts"])
    refresh_job(db, unit["job_id"], now)


def sweep(db, cfg: Config, now: float) -> None:
    """Recover every unit whose lease ran out."""
    for unit in db.execute("SELECT * FROM units WHERE status = 'assigned' AND lease_expires < ?", (now,)).fetchall():
        _recover_lost(db, cfg, unit, now, "lease expired")


def _owned_unit(db, cfg: Config, device_id: str, unit_id: str, now: float):
    """Return the device's assigned unit and renew its lease, or raise LeaseLost."""
    unit = db.execute("SELECT * FROM units WHERE unit_id = ?", (unit_id,)).fetchone()
    if unit is None or unit["status"] != "assigned" or unit["device_id"] != device_id:
        raise LeaseLost(unit_id)
    db.execute("UPDATE units SET lease_expires = ? WHERE unit_id = ?", (now + cfg.queue["lease_seconds"], unit_id))
    return unit


def _assign(db, cfg: Config, device_id: str, now: float) -> str | None:
    unit = db.execute(
        "SELECT units.* FROM units JOIN jobs USING (job_id)"
        " WHERE units.status = 'queued' AND jobs.status IN ('queued', 'running')"
        " ORDER BY jobs.created_at, units.position, units.created_at LIMIT 1"
    ).fetchone()
    if unit is None:
        return None
    db.execute(
        "UPDATE units SET status = 'assigned', device_id = ?, lease_expires = ?, assigned_at = ? WHERE unit_id = ?",
        (device_id, now + cfg.queue["lease_seconds"], now, unit["unit_id"]),
    )
    db.execute(
        "UPDATE jobs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE job_id = ?", (now, unit["job_id"])
    )
    return unit["unit_id"]


def unit_payload(db, cfg: Config, unit_id: str) -> dict:
    """The unit as the phone receives it (docs/API.md, Poll)."""
    unit = db.execute("SELECT * FROM units WHERE unit_id = ?", (unit_id,)).fetchone()
    job = get_job(db, unit["job_id"])
    run_settings = json.loads(job["settings_json"])["run"]
    runs = db.execute(
        "SELECT * FROM runs WHERE unit_id = ? AND status = 'pending' ORDER BY position", (unit_id,)
    ).fetchall()
    image_ids = list(dict.fromkeys(run["image_id"] for run in runs))
    prompt_ids = list(dict.fromkeys(run["prompt_id"] for run in runs))
    images = {
        row["image_id"]: row
        for row in db.execute(f"SELECT * FROM job_images WHERE image_id IN ({','.join('?' * len(image_ids))})", image_ids)
    }
    prompts = {
        row["prompt_id"]: row
        for row in db.execute(f"SELECT * FROM prompts WHERE prompt_id IN ({','.join('?' * len(prompt_ids))})", prompt_ids)
    }
    return {
        "unit_id": unit_id,
        "job_id": job["job_id"],
        "job_name": job["name"],
        "submitted_by": job["submitted_by"],
        "lease_s": cfg.queue["lease_seconds"],
        "framework": run_settings["framework"],
        "model": unit["model"],
        "backend": unit["backend"],
        "mode": run_settings["mode"],
        "warmup": run_settings["mode"] == "warm",
        "rest_seconds": run_settings["rest_seconds"],
        "max_tokens": run_settings["max_tokens"],
        "run_timeout_s": cfg.queue["run_timeout_seconds"],
        "images": [
            {
                "image_id": image_id,
                "url": f"/api/v1/device/images/{image_id}",
                "sha256": images[image_id]["sha256"],
                "bytes": images[image_id]["bytes"],
            }
            for image_id in image_ids
        ],
        "prompts": [{"prompt_id": prompt_id, "text": prompts[prompt_id]["text"]} for prompt_id in prompt_ids],
        "runs": [
            {"run_id": run["run_id"], "image_id": run["image_id"], "prompt_id": run["prompt_id"], "repetition": run["repetition"]}
            for run in runs
        ],
    }


# Phone calls -------------------------------------------------------------


def poll(db, cfg: Config, device_id: str, status: dict, now: float) -> dict:
    """Heartbeat plus request for work (docs/API.md, Poll). Returns the response minus the unit body."""
    sweep(db, cfg, now)
    db.execute(
        "UPDATE devices SET last_seen = ?, status_json = ? WHERE device_id = ?", (now, json.dumps(status), device_id)
    )
    unit_id = status.get("unit_id")
    cancel = False
    if unit_id is not None:
        unit = _owned_unit(db, cfg, device_id, unit_id, now)
        cancel = get_job(db, unit["job_id"])["status"] == "cancelled"
    else:
        held = db.execute(
            "SELECT * FROM units WHERE status = 'assigned' AND device_id = ?", (device_id,)
        ).fetchone()
        if held is not None:
            # The phone holds no unit but the server thinks it does: it restarted mid-unit.
            _recover_lost(db, cfg, held, now, "worker restarted during this unit")
    assigned = None
    if unit_id is None and status["state"] == "idle" and not cfg.queue["paused"]:
        assigned = _assign(db, cfg, device_id, now)
    return {"poll_after_s": cfg.queue["poll_seconds"], "cancel_unit": cancel, "unit_id": assigned}


def image_for_device(db, cfg: Config, device_id: str, image_id: str, now: float):
    """An image of a unit currently leased to this device, or NotFound."""
    sweep(db, cfg, now)
    image = db.execute(
        "SELECT job_images.* FROM job_images"
        " JOIN runs ON runs.image_id = job_images.image_id"
        " JOIN units ON units.unit_id = runs.unit_id"
        " WHERE job_images.image_id = ? AND units.status = 'assigned' AND units.device_id = ? LIMIT 1",
        (image_id, device_id),
    ).fetchone()
    if image is None or image["path"] is None:
        raise NotFound(image_id)
    return image


def report_run(db, cfg: Config, device_id: str, run_id: str, report: dict, now: float) -> bool:
    """Store one run's result; return whether the phone should continue the unit."""
    sweep(db, cfg, now)
    unit = _owned_unit(db, cfg, device_id, report["unit_id"], now)
    run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None or run["unit_id"] != unit["unit_id"]:
        raise LeaseLost(run_id)
    if run["status"] == "pending":
        # A repeated report of the same run keeps the first one.
        db.execute(
            "UPDATE runs SET status = ?, result_json = ?, reported_at = ?, error = ? WHERE run_id = ?",
            (
                "completed" if report["status"] == "completed" else "error",
                json.dumps(report),
                now,
                report.get("error"),
                run_id,
            ),
        )
    return get_job(db, unit["job_id"])["status"] != "cancelled"


def finish_unit(db, cfg: Config, device_id: str, unit_id: str, status: str, error: str | None, now: float) -> None:
    """Close a unit (docs/API.md, Finish)."""
    sweep(db, cfg, now)
    unit = _owned_unit(db, cfg, device_id, unit_id, now)
    job_id = unit["job_id"]
    if status == "cancelled":
        db.execute("UPDATE units SET status = 'cancelled', finished_at = ? WHERE unit_id = ?", (now, unit_id))
        db.execute("UPDATE runs SET status = 'cancelled' WHERE unit_id = ? AND status = 'pending'", (unit_id,))
        cancel_job(db, cfg, job_id, now)
    elif status == "failed":
        attempts = unit["attempts"] + 1
        db.execute(
            "UPDATE units SET status = 'failed', finished_at = ?, error = ? WHERE unit_id = ?", (now, error, unit_id)
        )
        if attempts >= cfg.queue["max_unit_attempts"]:
            message = f"{unit['model']} on {unit['backend']} failed {attempts} times: {error}"
            db.execute(
                "UPDATE runs SET status = 'error', error = ?, reported_at = ? WHERE unit_id = ? AND status = 'pending'",
                (message, now, unit_id),
            )
            db.execute("UPDATE jobs SET error = COALESCE(error, ?) WHERE job_id = ?", (message, job_id))
        else:
            _requeue(db, unit, now, attempts)
    else:
        db.execute(
            "UPDATE units SET status = ?, finished_at = ? WHERE unit_id = ?",
            ("done" if status == "done" else "released", now, unit_id),
        )
        # Runs a `done` unit left unreported are requeued rather than lost.
        _requeue(db, unit, now, unit["attempts"])
    refresh_job(db, job_id, now)


# Read models for the website --------------------------------------------


def device_status(db, cfg: Config, now: float) -> list[dict]:
    result = []
    for row in db.execute("SELECT * FROM devices WHERE revoked = 0 ORDER BY created_at"):
        status = json.loads(row["status_json"]) if row["status_json"] else {}
        online = row["last_seen"] is not None and now - row["last_seen"] <= 3 * cfg.queue["poll_seconds"]
        result.append(
            {
                "device_id": row["device_id"],
                "name": row["name"],
                "online": online,
                "last_seen": row["last_seen"],
                "state": status.get("state") if online else "offline",
                "unit_id": status.get("unit_id"),
                "status": status,
            }
        )
    return result


def job_summary(db, job) -> dict:
    counts = {
        row["status"]: row["n"]
        for row in db.execute("SELECT status, COUNT(*) AS n FROM runs WHERE job_id = ? GROUP BY status", (job["job_id"],))
    }
    settings = json.loads(job["settings_json"])
    return {
        "job_id": job["job_id"],
        "name": job["name"],
        "submitted_by": job["submitted_by"],
        "status": job["status"],
        "created_at": job["created_at"],
        "started_at": job["started_at"],
        "finished_at": job["finished_at"],
        "error": job["error"],
        "runs": {"total": sum(counts.values()), **{key: counts.get(key, 0) for key in ("pending", "completed", "error", "cancelled")}},
        "models": settings["run"]["models"],
        "backends": settings["run"]["backends"],
    }


def job_detail(db, job_id: str) -> dict:
    job = get_job(db, job_id)
    detail = job_summary(db, job)
    detail["settings"] = json.loads(job["settings_json"])
    detail["units"] = [
        {key: row[key] for key in ("unit_id", "model", "backend", "status", "attempts", "assigned_at", "finished_at", "error")}
        for row in db.execute("SELECT * FROM units WHERE job_id = ? ORDER BY position, created_at", (job_id,))
    ]
    return detail
