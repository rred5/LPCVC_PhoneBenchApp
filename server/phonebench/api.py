"""HTTP API: the phone endpoints from docs/API.md, plus the user endpoints.

Local mode has no sign-in: the server listens only on 127.0.0.1 and every job
belongs to the user "local". Cloud mode takes a Supabase sign-in token and
admits only the emails in the `members` table.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import re
import time
from typing import Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__, images, outputs, scheduler
from .auth import PROVIDERS, AuthError, SupabaseAuth
from .config import Config
from .settings import SettingsError, resolve
from .storage import INLINE_BYTES, FileTooLarge, UpstreamError, open_storage
from .store import new_id, open_store

JOB_NAME = re.compile(r"^[\w .()-]{1,80}$")
# Signed upload URLs last two hours, so a slot left unfinished for three can
# no longer receive a file and is safe to delete.
STALE_SLOT_SECONDS = 3 * 3600
STALE_UPLOAD_SECONDS = 24 * 3600
HOUSEKEEPING_SECONDS = 300
log = logging.getLogger("phonebench")
UPLOAD_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


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


def _incoming(upload_id: str) -> str:
    return f"incoming/{upload_id}"


def _prepared(upload_id: str) -> str:
    return f"images/{upload_id}.png"


@dataclass(frozen=True)
class User:
    email: str
    is_admin: bool


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


class FileToUpload(BaseModel):
    name: str = Field(max_length=1_000)
    bytes: int


class UploadRequest(BaseModel):
    max_edge: int | None = None
    files: list[FileToUpload] = Field(min_length=1, max_length=500)


class JobRequest(BaseModel):
    name: str
    settings: dict


# App ---------------------------------------------------------------------


def create_app(cfg: Config, clock=time.time, store=None, storage=None, auth=None) -> FastAPI:
    store = store or open_store(cfg)
    storage = storage or open_storage(cfg)
    if auth is None and cfg.cloud:
        auth = SupabaseAuth(cfg.supabase_url, cfg.supabase_key)
    app = FastAPI(title="LPCVC PhoneBench", version=__version__, docs_url=None if cfg.cloud else "/docs", redoc_url=None)
    app.state.store, app.state.cfg, app.state.clock, app.state.storage = store, cfg, clock, storage
    limits = cfg.limits

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

    @app.exception_handler(UpstreamError)
    def _upstream(_: Request, error: UpstreamError):
        return _error(502, "storage_error", "File storage or sign-in is not answering; try again shortly")

    @app.exception_handler(Exception)
    def _unexpected(_: Request, error: Exception):
        return _error(500, "server_error", "Something went wrong on the server")

    def current_user(authorization: str | None = Header(default=None)) -> User:
        if auth is None:
            return User("local", True)
        if not authorization or not authorization.startswith("Bearer "):
            raise ApiError(401, "unauthorized", "Sign in first")
        try:
            email = auth.email_for(authorization.removeprefix("Bearer ").strip())
        except AuthError as error:
            raise ApiError(401, "unauthorized", str(error)) from None
        member = store.member(email)
        if member is None:
            raise ApiError(403, "not_a_member", f"{email} is not on the PhoneBench member list; ask the admin to add you")
        return User(member["email"], bool(member["is_admin"]))

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

    def finalize_job(job_id: str) -> None:
        """Delete a finished job's images; local mode also writes its output files."""
        keep = cfg.queue["keep_images_after_job"]
        if not storage.cloud:
            outputs.write_outputs(store, cfg, job_id)
        with store.read() as db:
            paths = [row["path"] for row in db.execute("SELECT path FROM job_images WHERE job_id = ? AND path IS NOT NULL", (job_id,))]
        if not keep:
            storage.delete(paths)
        with store.tx() as db:
            db.execute("UPDATE jobs SET outputs_at = ? WHERE job_id = ?", (now(), job_id))
            if not keep:
                db.execute("UPDATE job_images SET path = NULL WHERE job_id = ?", (job_id,))

    app.state.next_housekeeping = 0.0

    def cleanup() -> None:
        """Finalize finished jobs and clear stale uploads.

        Runs after the request's own transaction has committed, so it must never
        fail the request: a phone whose poll got an error would poll again as
        if it had lost the unit. Anything that fails is retried next time.
        """
        try:
            with store.read() as db:
                job_ids = scheduler.jobs_needing_outputs(db)
        except Exception:
            log.exception("cleanup: could not list finished jobs")
            return
        for job_id in job_ids:
            try:
                finalize_job(job_id)
            except Exception:
                log.exception("cleanup: could not finalize %s", job_id)
        if now() >= app.state.next_housekeeping:
            app.state.next_housekeeping = now() + HOUSEKEEPING_SECONDS
            try:
                purge_stale_uploads()
            except Exception:
                log.exception("cleanup: could not clear stale uploads")

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
        cleanup()
        if lost:
            raise ApiError(409, "lease_lost", "This unit is no longer assigned to the phone")
        return result

    @app.get("/api/v1/device/images/{image_id}")
    def device_image(image_id: str, device_id: str = Depends(current_device)):
        with store.tx() as db:
            try:
                image = scheduler.image_for_device(db, cfg, device_id, image_id, now())
            except scheduler.NotFound:
                raise ApiError(404, "not_found", "No such image in a unit leased to this phone") from None
        if storage.cloud:
            # The phone checks the SHA-256 from the unit, so the URL needs no further trust.
            return RedirectResponse(storage.download_url(image["path"]), status_code=307)
        data = storage.get(image["path"], image["bytes"])
        if data is None:
            raise ApiError(404, "not_found", "The image file is missing")
        return Response(data, media_type="image/png")

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
        cleanup()
        if lost:
            raise ApiError(409, "lease_lost", "This unit is no longer assigned to the phone")
        return {"ok": True}

    # Who is signed in, and what they can choose ---------------------------

    @app.get("/api/v1/auth-config")
    def auth_config():
        """What the website needs to sign people in; public values only."""
        return {
            "cloud": cfg.cloud,
            "supabase_url": cfg.supabase_url,
            "supabase_key": cfg.supabase_publishable_key,
            "providers": list(PROVIDERS),
        }

    @app.get("/api/v1/me")
    def me(user: User = Depends(current_user)):
        return {"email": user.email, "is_admin": user.is_admin, "cloud": cfg.cloud}

    @app.get("/api/v1/options")
    def options(user: User = Depends(current_user)):
        """What the job form needs: defaults, limits and the model list."""
        return {
            "defaults": cfg.defaults,
            "limits": {
                key: limits[key]
                for key in (
                    "max_images", "max_prompts", "max_prompt_chars", "repetitions", "rest_seconds", "max_edge",
                    "max_tokens", "max_upload_bytes", "max_upload_edge", "max_unfinished_uploads", "max_runs_per_job",
                    "max_queued_jobs_per_user",
                )
            },
            "models": [
                {key: model[key] for key in ("id", "display_name", "backends")} for model in cfg.models
            ],
            "upload_types": sorted(UPLOAD_TYPES),
        }

    # Uploads: ask for slots, send each file, then complete it -------------

    def storage_used(db) -> int:
        """Bytes in storage now, plus originals that are on their way."""
        waiting = db.execute("SELECT COALESCE(SUM(declared_bytes), 0) FROM uploads WHERE status = 'waiting'").fetchone()[0]
        ready = db.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM uploads WHERE status = 'ready' AND job_id IS NULL"
        ).fetchone()[0]
        in_jobs = db.execute("SELECT COALESCE(SUM(bytes), 0) FROM job_images WHERE path IS NOT NULL").fetchone()[0]
        return int(waiting) + int(ready) + int(in_jobs)

    def purge_stale_uploads() -> None:
        """Delete slots unfinished for 3 hours and uploads unused for a day."""
        with store.read() as db:
            stale = [
                row["upload_id"]
                for row in db.execute(
                    "SELECT upload_id FROM uploads WHERE job_id IS NULL"
                    " AND ((status = 'waiting' AND created_at < ?) OR created_at < ?)",
                    (now() - STALE_SLOT_SECONDS, now() - STALE_UPLOAD_SECONDS),
                )
            ]
        if stale:
            discard_uploads(stale)

    def discard_uploads(upload_ids: list[str]) -> None:
        # Files first: if storage fails, the rows stay and the next try finds them.
        storage.delete([key for upload_id in upload_ids for key in (_incoming(upload_id), _prepared(upload_id))])
        with store.tx() as db:
            for upload_id in upload_ids:
                db.execute("DELETE FROM uploads WHERE upload_id = ? AND job_id IS NULL", (upload_id,))

    def own_upload(db, upload_id: str, user: User):
        row = db.execute("SELECT * FROM uploads WHERE upload_id = ?", (upload_id,)).fetchone()
        if row is None or row["uploaded_by"] != user.email:
            raise ApiError(404, "not_found", "No such upload")
        return row

    @app.post("/api/v1/uploads")
    def create_uploads(body: UploadRequest, user: User = Depends(current_user)):
        """Reserve one slot per file. Send each file to its `upload_url` with PUT, then complete it."""
        max_edge = body.max_edge if body.max_edge is not None else cfg.defaults["preprocessing"]["max_edge"]
        low, high = limits["max_edge"]
        if not low <= max_edge <= high:
            raise ApiError(422, "invalid_request", f"max_edge must be between {low} and {high}")
        cap = limits["max_unfinished_uploads"]
        try:
            purge_stale_uploads()
        except UpstreamError:
            log.exception("could not clear stale uploads")
        slots, rejected = [], []
        with store.tx() as db:
            unfinished = db.execute(
                "SELECT COUNT(*) FROM uploads WHERE uploaded_by = ? AND status = 'waiting'", (user.email,)
            ).fetchone()[0]
            if unfinished + len(body.files) > cap:
                raise ApiError(
                    429, "too_many_uploads",
                    f"At most {cap} unfinished uploads at a time; send and complete these ({unfinished} open) first",
                )
            used = storage_used(db)
            for file in body.files:
                try:
                    name = images.clean_name(file.name)
                    if not name.lower().endswith(tuple(UPLOAD_TYPES)):
                        raise images.ImageError("only PNG, JPEG and WebP images are accepted")
                    if not 0 < file.bytes <= limits["max_upload_bytes"]:
                        raise images.ImageError(f"file is larger than {limits['max_upload_bytes'] // (1024 * 1024)} MB")
                    if used + file.bytes > limits["storage_budget_bytes"]:
                        raise images.ImageError("server storage is full; wait for running jobs to finish")
                except images.ImageError as error:
                    rejected.append({"name": file.name[:255], "reason": str(error)})
                    continue
                upload_id = new_id("up")
                db.execute(
                    "INSERT INTO uploads (upload_id, name, label, uploaded_by, created_at, max_edge, declared_bytes, status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'waiting')",
                    (upload_id, name, images.label_for(name), user.email, now(), max_edge, file.bytes),
                )
                used += file.bytes
                slots.append({"upload_id": upload_id, "name": name, "label": images.label_for(name)})
        try:
            # One storage call per file; in parallel so a batch takes about as long as one.
            with ThreadPoolExecutor(max_workers=8) as pool:
                urls = list(pool.map(lambda slot: storage.upload_url(_incoming(slot["upload_id"])), slots))
        except Exception:
            discard_uploads([slot["upload_id"] for slot in slots])
            raise
        for slot, url in zip(slots, urls):
            slot["upload_url"] = url or f"/api/v1/uploads/{slot['upload_id']}/data"
            slot["content_type"] = UPLOAD_TYPES[next(ext for ext in UPLOAD_TYPES if slot["name"].lower().endswith(ext))]
        return {"uploads": slots, "rejected": rejected, "max_edge": max_edge}

    @app.delete("/api/v1/uploads/{upload_id}")
    def delete_upload(upload_id: str, user: User = Depends(current_user)):
        """Drop an upload that is not part of a job, e.g. one the browser gave up on."""
        with store.read() as db:
            row = own_upload(db, upload_id, user)
        if row["job_id"]:
            raise ApiError(409, "in_a_job", "This image belongs to a job")
        discard_uploads([upload_id])
        return {"ok": True}

    @app.put("/api/v1/uploads/{upload_id}/data")
    async def upload_data(upload_id: str, request: Request, user: User = Depends(current_user)):
        """Local mode's stand-in for the bucket's signed upload URL."""
        if storage.cloud:
            raise ApiError(404, "not_found", "Upload to the upload_url you were given")
        with store.read() as db:
            row = own_upload(db, upload_id, user)
        if row["status"] != "waiting":
            raise ApiError(409, "already_complete", "This upload is already complete")
        data = bytearray()
        async for chunk in request.stream():
            data += chunk
            if len(data) > row["declared_bytes"]:
                raise ApiError(413, "too_large", "File is larger than its declared size")
        storage.put(_incoming(upload_id), bytes(data), request.headers.get("content-type") or "application/octet-stream")
        return {"ok": True}

    @app.post("/api/v1/uploads/{upload_id}/complete")
    def complete_upload(upload_id: str, user: User = Depends(current_user)):
        """Check the uploaded file and prepare it for the phone; the original is then deleted."""
        with store.read() as db:
            row = own_upload(db, upload_id, user)
        if row["status"] != "waiting":
            return upload_info(row)
        try:
            # The storage budget counted the declared size, so hold the file to it.
            data = storage.get(_incoming(upload_id), row["declared_bytes"])
            if data is None:
                raise ApiError(409, "not_uploaded", "The file has not arrived yet; send it to its upload_url first")
            checked = images.check_upload(data, limits["max_upload_bytes"], limits["max_upload_edge"])
            png, meta = images.prepare(data, row["max_edge"])
        except (images.ImageError, FileTooLarge) as error:
            reason = str(error) if isinstance(error, images.ImageError) else "file is larger than its declared size"
            with store.tx() as db:
                db.execute("DELETE FROM uploads WHERE upload_id = ?", (upload_id,))
            storage.delete([_incoming(upload_id)])
            raise ApiError(422, "bad_image", f"{row['name']}: {reason}") from None
        storage.put(_prepared(upload_id), png, "image/png")
        storage.delete([_incoming(upload_id)])
        width, height = meta["output_size"]
        with store.tx() as db:
            db.execute(
                "UPDATE uploads SET status = 'ready', path = ?, sha256 = ?, bytes = ?, width = ?, height = ?,"
                " source_sha256 = ?, source_bytes = ?, source_width = ?, source_height = ? WHERE upload_id = ? AND status = 'waiting'",
                (
                    _prepared(upload_id), meta["output_sha256"], len(png), width, height,
                    checked.sha256, checked.bytes, checked.width, checked.height, upload_id,
                ),
            )
            row = own_upload(db, upload_id, user)
        return upload_info(row)

    def upload_info(row) -> dict:
        return {
            key: row[key]
            for key in ("upload_id", "name", "label", "status", "max_edge", "width", "height", "bytes", "source_width", "source_height", "source_bytes")
        }

    # Jobs ------------------------------------------------------------------

    @app.post("/api/v1/jobs", status_code=201)
    def create_job(body: JobRequest, user: User = Depends(current_user)):
        if not JOB_NAME.match(body.name):
            raise ApiError(422, "invalid_name", "Job names use 1–80 letters, digits, spaces, dots, dashes, underscores or brackets")
        try:
            settings = resolve(body.settings, cfg)
        except SettingsError as error:
            raise ApiError(422, "invalid_settings", str(error)) from error
        upload_ids = settings["run"]["images"]
        max_edge = settings["preprocessing"]["max_edge"]
        job_id = new_id("j")
        with store.tx() as db:
            if scheduler.active_jobs_for(db, user.email) >= limits["max_queued_jobs_per_user"]:
                raise ApiError(429, "too_many_jobs", f"At most {limits['max_queued_jobs_per_user']} queued or running jobs per person")
            rows = {
                row["upload_id"]: row
                for row in db.execute(f"SELECT * FROM uploads WHERE upload_id IN ({','.join('?' * len(upload_ids))})", upload_ids)
            }
            unusable = [
                i for i in upload_ids
                if i not in rows or rows[i]["job_id"] or rows[i]["uploaded_by"] != user.email or rows[i]["status"] != "ready"
            ]
            if unusable:
                raise ApiError(422, "invalid_settings", f"Unknown, unfinished or already used images: {', '.join(unusable[:5])}")
            wrong_size = [rows[i]["name"] for i in upload_ids if rows[i]["max_edge"] != max_edge]
            if wrong_size:
                raise ApiError(
                    422, "invalid_settings",
                    f"{wrong_size[0]} was prepared for a different max_edge; upload the images again with max_edge {max_edge}",
                )
            prepared = [
                {
                    "image_id": new_id("img"), "name": rows[i]["name"], "label": rows[i]["label"], "path": rows[i]["path"],
                    "sha256": rows[i]["sha256"], "bytes": rows[i]["bytes"], "width": rows[i]["width"],
                    "height": rows[i]["height"], "source_sha256": rows[i]["source_sha256"],
                }
                for i in upload_ids
            ]
            estimate = scheduler.estimate_seconds(db, cfg, settings)
            scheduler.insert_job(db, job_id, body.name, user.email, settings, prepared, now())
            db.execute(f"UPDATE uploads SET job_id = ? WHERE upload_id IN ({','.join('?' * len(upload_ids))})", [job_id, *upload_ids])
            summary = scheduler.job_summary(db, scheduler.get_job(db, job_id))
        return {**summary, "estimated_seconds": round(estimate)}

    @app.get("/api/v1/jobs")
    def list_jobs(user: User = Depends(current_user)):
        with store.read() as db:
            jobs = db.execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200").fetchall()
            return {"jobs": scheduler.job_summaries(db, jobs)}

    def _job_or_404(db, job_id: str):
        try:
            return scheduler.get_job(db, job_id)
        except scheduler.NotFound:
            raise ApiError(404, "not_found", f"No job {job_id}") from None

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str, user: User = Depends(current_user)):
        with store.read() as db:
            _job_or_404(db, job_id)
            return scheduler.job_detail(db, job_id)

    @app.get("/api/v1/jobs/{job_id}/runs")
    def get_runs(job_id: str, user: User = Depends(current_user)):
        with store.read() as db:
            _job_or_404(db, job_id)
        _, rows = outputs.collect(store, job_id)
        return {"runs": [{key: value for key, value in row.items() if key != "_raw"} for row in rows]}

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel(job_id: str, user: User = Depends(current_user)):
        with store.tx() as db:
            job = _job_or_404(db, job_id)
            if job["submitted_by"] != user.email and not user.is_admin:
                raise ApiError(403, "not_yours", "Only the person who submitted a job, or an admin, can cancel it")
            scheduler.cancel_job(db, cfg, job_id, now())
        cleanup()
        with store.read() as db:
            return scheduler.job_summary(db, scheduler.get_job(db, job_id))

    @app.get("/api/v1/jobs/{job_id}/outputs/{name}")
    def get_output(job_id: str, name: str, user: User = Depends(current_user)):
        if name not in outputs.OUTPUT_FILES:
            raise ApiError(404, "not_found", f"Outputs are: {', '.join(outputs.OUTPUT_FILES)}")
        with store.read() as db:
            _job_or_404(db, job_id)
        files = outputs.build_files(store, job_id)
        data = outputs.zip_files(files, job_id) if name == "outputs.zip" else files.get(name)
        if data is None:
            raise ApiError(404, "not_found", f"This job has no {name}")
        filename = f"{job_id}-{name}"
        media = {"csv": "text/csv", "zip": "application/zip"}.get(name.rsplit(".", 1)[-1], "application/json")
        if storage.cloud and len(data) > INLINE_BYTES:
            key = f"outputs/{job_id}/{name}"
            storage.put(key, data, media)
            return RedirectResponse(storage.download_url(key, filename), status_code=307)
        return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @app.get("/api/v1/device/status")
    def status(user: User = Depends(current_user)):
        with store.read() as db:
            return {"devices": scheduler.device_status(db, cfg, now())}

    @app.get("/api/v1/health")
    def health():
        return {"ok": True, "version": __version__}

    return app
