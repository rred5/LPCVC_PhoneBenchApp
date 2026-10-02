"""HTTP API: the phone endpoints from docs/API.md, plus the user endpoints.

Local mode has no sign-in: the server listens only on 127.0.0.1 and every job
belongs to the user "local". Phase 3 adds sign-in in `current_user`.
"""

from pathlib import Path
import re
import shutil
import time
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, images, outputs, scheduler
from .config import Config
from .settings import SettingsError, resolve
from .store import Store, new_id

JOB_NAME = re.compile(r"^[\w .()-]{1,80}$")
STALE_UPLOAD_SECONDS = 24 * 3600


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _version(text: str | None) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in (text or "").split("."))
    except ValueError:
        return None


# Request bodies ----------------------------------------------------------


class _Body(BaseModel):
    # Newer apps may send fields this server does not know yet.
    model_config = ConfigDict(extra="ignore")


class PollRequest(_Body):
    state: Literal["idle", "downloading", "running", "paused", "error"]
    unit_id: str | None = None
    app_version: str | None = None
    framework: str | None = None
    framework_version: str | None = None
    models_downloaded: list[str] = []
    battery_c: float | None = None
    thermal_status: str | None = None
    free_memory_mb: float | None = None


class Metrics(_Body):
    ttft_ms: float | None = None
    prompt_tokens: int | None = None
    generated_tokens: int | None = None
    prefill_tok_s: float | None = None
    decode_tok_s: float | None = None
    stop_reason: str | None = None


class RunReport(_Body):
    unit_id: str
    status: Literal["completed", "error"]
    started_at: str | None = None
    finished_at: str | None = None
    response: str | None = Field(default=None, max_length=200_000)
    error: str | None = Field(default=None, max_length=5_000)
    metrics: Metrics | None = None
    load_ms: float | None = None
    warm: bool | None = None
    framework: str | None = None
    framework_version: str | None = None
    runtime: str | None = None
    compute_unit: str | None = None
    battery_c: float | None = None
    thermal_status: str | None = None
    free_memory_mb: float | None = None


class FinishRequest(_Body):
    status: Literal["done", "released", "cancelled", "failed"]
    error: str | None = Field(default=None, max_length=5_000)


class JobRequest(BaseModel):
    name: str
    settings: dict


# App ---------------------------------------------------------------------


def create_app(cfg: Config, clock=time.time) -> FastAPI:
    store = Store(cfg.db_path)
    cfg.uploads_dir.mkdir(parents=True, exist_ok=True)
    cfg.jobs_dir.mkdir(parents=True, exist_ok=True)
    app = FastAPI(title="LPCVC PhoneBench", version=__version__)
    app.state.store, app.state.cfg, app.state.clock = store, cfg, clock

    def now() -> float:
        return app.state.clock()

    @app.exception_handler(ApiError)
    def _api_error(_: Request, error: ApiError):
        return _error(error.status, error.code, error.message)

    @app.exception_handler(RequestValidationError)
    def _invalid(_: Request, error: RequestValidationError):
        first = error.errors()[0] if error.errors() else {}
        where = ".".join(str(part) for part in first.get("loc", ()))
        return _error(422, "invalid_request", f"{where}: {first.get('msg', 'invalid request')}")

    @app.exception_handler(StarletteHTTPException)
    def _http(_: Request, error: StarletteHTTPException):
        return _error(error.status_code, "http_error", str(error.detail))

    def current_user() -> str:
        return "local"

    def current_device(
        authorization: str | None = Header(default=None),
        app_version: str | None = Header(default=None, alias="PhoneBench-App-Version"),
    ) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "unauthorized", "Missing device key")
        device = store.device_for_key(authorization.removeprefix("Bearer ").strip())
        if device is None:
            raise ApiError(401, "unauthorized", "Device key rejected")
        version = _version(app_version)
        if version is None or version < _version(cfg.min_app_version):
            raise ApiError(426, "app_too_old", f"PhoneBench {cfg.min_app_version} or newer is required")
        return device["device_id"]

    def finalize_finished_jobs() -> None:
        """Write outputs for newly finished jobs and delete their images."""
        with store.tx() as db:
            job_ids = scheduler.jobs_needing_outputs(db)
        keep = cfg.queue["keep_images_after_job"]
        for job_id in job_ids:
            outputs.write_outputs(store, cfg, job_id)
            if not keep:
                shutil.rmtree(cfg.jobs_dir / job_id / "images", ignore_errors=True)
            with store.tx() as db:
                db.execute("UPDATE jobs SET outputs_at = ? WHERE job_id = ?", (now(), job_id))
                if not keep:
                    db.execute("UPDATE job_images SET path = NULL WHERE job_id = ?", (job_id,))

    # Phone endpoints -----------------------------------------------------

    @app.post("/api/v1/device/poll")
    def device_poll(body: PollRequest, device_id: str = Depends(current_device)):
        lost = False
        with store.tx() as db:
            try:
                result = scheduler.poll(db, cfg, device_id, body.model_dump(), now())
                unit_id = result.pop("unit_id")
                result["unit"] = scheduler.unit_payload(db, cfg, unit_id) if unit_id else None
            except scheduler.LeaseLost:
                # Commit the heartbeat and any recovery, then tell the phone.
                lost = True
        finalize_finished_jobs()
        if lost:
            raise ApiError(409, "lease_lost", "This unit is no longer assigned to the phone")
        return result

    @app.get("/api/v1/device/images/{image_id}")
    def device_image(image_id: str, device_id: str = Depends(current_device)):
        with store.tx() as db:
            try:
                image = scheduler.image_for_device(db, cfg, device_id, image_id, now())
            except scheduler.NotFound:
                image = None
        if image is None or not Path(image["path"]).is_file():
            raise ApiError(404, "not_found", "No such image in a unit leased to this phone")
        return FileResponse(image["path"], media_type="image/png")

    @app.post("/api/v1/device/runs/{run_id}")
    def device_run(run_id: str, body: RunReport, device_id: str = Depends(current_device)):
        lost = False
        with store.tx() as db:
            try:
                keep_going = scheduler.report_run(db, cfg, device_id, run_id, body.model_dump(), now())
            except scheduler.LeaseLost:
                lost = True
        if lost:
            raise ApiError(409, "lease_lost", "This run is not part of a unit leased to the phone")
        return {"continue": keep_going}

    @app.post("/api/v1/device/units/{unit_id}/finish")
    def device_finish(unit_id: str, body: FinishRequest, device_id: str = Depends(current_device)):
        lost = False
        with store.tx() as db:
            try:
                scheduler.finish_unit(db, cfg, device_id, unit_id, body.status, body.error, now())
            except scheduler.LeaseLost:
                lost = True
        finalize_finished_jobs()
        if lost:
            raise ApiError(409, "lease_lost", "This unit is no longer assigned to the phone")
        return {"ok": True}

    # User endpoints ------------------------------------------------------

    def storage_used(db) -> int:
        uploads = db.execute("SELECT COALESCE(SUM(bytes), 0) FROM uploads WHERE job_id IS NULL").fetchone()[0]
        prepared = db.execute("SELECT COALESCE(SUM(bytes), 0) FROM job_images WHERE path IS NOT NULL").fetchone()[0]
        return uploads + prepared

    def purge_stale_uploads(db) -> None:
        cutoff = now() - STALE_UPLOAD_SECONDS
        for row in db.execute("SELECT * FROM uploads WHERE job_id IS NULL AND created_at < ?", (cutoff,)).fetchall():
            Path(row["path"]).unlink(missing_ok=True)
            db.execute("DELETE FROM uploads WHERE upload_id = ?", (row["upload_id"],))

    @app.post("/api/v1/uploads")
    def upload(
        files: list[UploadFile] = File(...),
        paths: list[str] | None = Form(default=None),
        user: str = Depends(current_user),
    ):
        """Accept a folder of images; `paths` carries each file's path within the folder."""
        if paths is not None and len(paths) != len(files):
            raise ApiError(400, "invalid_request", "Send one path per file")
        limits = cfg.limits
        with store.tx() as db:
            purge_stale_uploads(db)
            used = storage_used(db)
        accepted, rejected = [], []
        for index, file in enumerate(files):
            raw_name = paths[index] if paths is not None else (file.filename or "")
            try:
                name = images.clean_name(raw_name)
                data = file.file.read(limits["max_upload_bytes"] + 1)
                checked = images.check_upload(data, limits["max_upload_bytes"], limits["max_upload_edge"])
                if used + checked.bytes > limits["storage_budget_bytes"]:
                    raise images.ImageError("server storage is full; wait for running jobs to finish")
            except images.ImageError as error:
                rejected.append({"name": str(raw_name)[:255], "reason": str(error)})
                continue
            upload_id = new_id("up")
            path = cfg.uploads_dir / upload_id
            path.write_bytes(data)
            label = images.label_for(name)
            with store.tx() as db:
                db.execute(
                    "INSERT INTO uploads (upload_id, name, label, path, sha256, bytes, width, height, uploaded_by, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (upload_id, name, label, str(path), checked.sha256, checked.bytes, checked.width, checked.height, user, now()),
                )
            used += checked.bytes
            accepted.append(
                {"upload_id": upload_id, "name": name, "label": label, "width": checked.width, "height": checked.height, "bytes": checked.bytes}
            )
        return {"accepted": accepted, "rejected": rejected}

    @app.post("/api/v1/jobs", status_code=201)
    def create_job(body: JobRequest, user: str = Depends(current_user)):
        if not JOB_NAME.match(body.name):
            raise ApiError(422, "invalid_name", "Job names use 1–80 letters, digits, spaces, dots, dashes, underscores or brackets")
        try:
            settings = resolve(body.settings, cfg)
        except SettingsError as error:
            raise ApiError(422, "invalid_settings", str(error)) from error
        upload_ids = settings["run"]["images"]
        with store.tx() as db:
            if scheduler.active_jobs_for(db, user) >= cfg.limits["max_queued_jobs_per_user"]:
                raise ApiError(429, "too_many_jobs", f"At most {cfg.limits['max_queued_jobs_per_user']} queued or running jobs per person")
            rows = {
                row["upload_id"]: dict(row)
                for row in db.execute(
                    f"SELECT * FROM uploads WHERE upload_id IN ({','.join('?' * len(upload_ids))})", upload_ids
                )
            }
            unusable = [i for i in upload_ids if i not in rows or rows[i]["job_id"] or rows[i]["uploaded_by"] != user]
            if unusable:
                raise ApiError(422, "invalid_settings", f"Unknown or already used images: {', '.join(unusable[:5])}")
            estimate = scheduler.estimate_seconds(db, cfg, settings)

        job_id = new_id("j")
        image_dir = cfg.jobs_dir / job_id / "images"
        image_dir.mkdir(parents=True)
        try:
            prepared = []
            for upload_id in upload_ids:
                row = rows[upload_id]
                png, meta = images.prepare(Path(row["path"]).read_bytes(), settings["preprocessing"]["max_edge"])
                image_id = new_id("img")
                path = image_dir / f"{image_id}.png"
                path.write_bytes(png)
                width, height = meta["output_size"]
                prepared.append(
                    {
                        "image_id": image_id, "name": row["name"], "label": row["label"], "path": str(path),
                        "sha256": meta["output_sha256"], "bytes": len(png), "width": width, "height": height,
                        "source_sha256": meta["source_sha256"],
                    }
                )
            with store.tx() as db:
                taken = db.execute(
                    f"SELECT COUNT(*) FROM uploads WHERE job_id IS NOT NULL AND upload_id IN ({','.join('?' * len(upload_ids))})",
                    upload_ids,
                ).fetchone()[0]
                if taken:
                    raise ApiError(409, "images_taken", "Another job used these images first")
                scheduler.insert_job(db, job_id, body.name, user, settings, prepared, now())
                db.execute(
                    f"UPDATE uploads SET job_id = ? WHERE upload_id IN ({','.join('?' * len(upload_ids))})",
                    [job_id, *upload_ids],
                )
        except BaseException:
            shutil.rmtree(cfg.jobs_dir / job_id, ignore_errors=True)
            raise
        # Originals are no longer needed once prepared.
        for row in rows.values():
            Path(row["path"]).unlink(missing_ok=True)
        with store.tx() as db:
            summary = scheduler.job_summary(db, scheduler.get_job(db, job_id))
        return {**summary, "estimated_seconds": round(estimate)}

    @app.get("/api/v1/jobs")
    def list_jobs(user: str = Depends(current_user)):
        with store.tx() as db:
            jobs = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200").fetchall()
            return {"jobs": [scheduler.job_summary(db, job) for job in jobs]}

    def _job_or_404(db, job_id: str):
        try:
            return scheduler.get_job(db, job_id)
        except scheduler.NotFound:
            raise ApiError(404, "not_found", f"No job {job_id}") from None

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str, user: str = Depends(current_user)):
        with store.tx() as db:
            _job_or_404(db, job_id)
            return scheduler.job_detail(db, job_id)

    @app.get("/api/v1/jobs/{job_id}/runs")
    def get_runs(job_id: str, user: str = Depends(current_user)):
        with store.tx() as db:
            _job_or_404(db, job_id)
        _, rows = outputs.collect(store, job_id)
        return {"runs": [{key: value for key, value in row.items() if key != "_raw"} for row in rows]}

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, user: str = Depends(current_user)):
        with store.tx() as db:
            _job_or_404(db, job_id)
            scheduler.cancel_job(db, cfg, job_id, now())
        finalize_finished_jobs()
        with store.tx() as db:
            return scheduler.job_summary(db, scheduler.get_job(db, job_id))

    @app.get("/api/v1/jobs/{job_id}/outputs/{name}")
    def get_output(job_id: str, name: str, user: str = Depends(current_user)):
        if name not in outputs.OUTPUT_FILES:
            raise ApiError(404, "not_found", f"Outputs are: {', '.join(outputs.OUTPUT_FILES)}")
        with store.tx() as db:
            job = _job_or_404(db, job_id)
        if job["outputs_at"] is None:
            # Still running: show the results so far.
            outputs.write_outputs(store, cfg, job_id)
        path = outputs.outputs_dir(cfg, job_id) / name
        if not path.is_file():
            raise ApiError(404, "not_found", f"This job has no {name}")
        media = "text/csv" if name.endswith(".csv") else "application/json"
        return FileResponse(path, media_type=media, filename=f"{job_id}-{name}")

    @app.get("/api/v1/device/status")
    def status(user: str = Depends(current_user)):
        with store.tx() as db:
            return {"devices": scheduler.device_status(db, cfg, now())}

    @app.get("/api/v1/health")
    def health():
        return {"ok": True, "version": __version__}

    return app
