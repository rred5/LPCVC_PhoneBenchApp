"""Command line: `python -m phonebench <command>`.

    serve        run the server on 127.0.0.1
    device-key   create (or replace) the phone's device key
    submit       upload a folder of images and submit a job
    status       list jobs, or show one
    fake-worker  run jobs with a simulated phone
    init-db      cloud: apply supabase/migrations (tables and storage bucket)
    members      cloud: list, add or remove who may sign in

`--env FILE` loads settings such as PHONEBENCH_DATABASE_URL from a file
(see server/cloud.env.example); without it everything stays local.
"""

import argparse
import json
import os
from pathlib import Path
import sys
import time

import httpx

from .config import load_config, read_env_file
from .store import open_store

DEFAULT_SERVER = "http://127.0.0.1:8000"
UPLOAD_BATCH = 20  # the server allows 20 unfinished uploads per person


def _print_json(value) -> None:
    print(json.dumps(value, indent=2))


def _key_path(cfg) -> Path:
    # Keep the cloud key apart so it never overwrites the local one.
    return cfg.data_dir / ("device_key_cloud.txt" if cfg.database_url else "device_key.txt")


def _write_key(cfg, key: str) -> Path:
    path = _key_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(key + "\n", encoding="utf-8")
    return path


def cmd_serve(args) -> None:
    import uvicorn

    from .api import create_app

    cfg = load_config(args.data)
    store = open_store(cfg)
    if not cfg.database_url and not store.has_active_device():
        path = _write_key(cfg, store.create_device("phone", time.time()))
        print(f"Created a device key; it is saved in {path}")
    if args.host not in ("127.0.0.1", "localhost") and not cfg.cloud:
        print("Warning: local mode has no sign-in; only listen on 127.0.0.1.", file=sys.stderr)
    uvicorn.run(create_app(cfg, store=store), host=args.host, port=args.port, log_level="info")


def cmd_device_key(args) -> None:
    cfg = load_config(args.data)
    store = open_store(cfg)
    path = _write_key(cfg, store.create_device(args.name, time.time()))
    store.close()
    print(f"New device key saved in {path}; the old key no longer works.")


def cmd_init_db(args) -> None:
    from .store import PostgresStore

    cfg = load_config(args.data)
    if not cfg.database_url:
        sys.exit("init-db is for cloud mode: pass --env with PHONEBENCH_DATABASE_URL set")
    store = PostgresStore(cfg.database_url)
    store.create_schema()
    store.close()
    print("Applied supabase/migrations: tables, row level security and the storage bucket are ready.")


def cmd_members(args) -> None:
    cfg = load_config(args.data)
    store = open_store(cfg)
    if args.action == "add":
        for email in args.emails:
            store.add_member(email, args.admin, time.time())
            print(f"Added {email.strip().lower()}" + (" as admin" if args.admin else ""))
    elif args.action == "remove":
        for email in args.emails:
            print(("Removed " if store.remove_member(email) else "Not a member: ") + email.strip().lower())
    else:
        for member in store.members():
            print(member["email"] + ("  (admin)" if member["is_admin"] else ""))
    store.close()


def _image_files(folder: Path) -> list[Path]:
    return sorted(
        path for path in folder.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(folder).parts)
    )


def _client(args, timeout: float) -> httpx.Client:
    token = args.token or os.environ.get("PHONEBENCH_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.Client(base_url=args.server, timeout=timeout, headers=headers)


def _fail(response: httpx.Response, what: str) -> None:
    if response.status_code >= 400:
        try:
            message = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            message = response.text[:300]
        sys.exit(f"{what} failed ({response.status_code}): {message}")


def upload_folder(client: httpx.Client, folder: Path, files: list[Path], max_edge: int | None) -> list[dict]:
    """Upload images with the slot / send / complete flow; return the completed uploads."""
    accepted = []
    for start in range(0, len(files), UPLOAD_BATCH):
        batch = {path.relative_to(folder).as_posix(): path for path in files[start:start + UPLOAD_BATCH]}
        response = client.post(
            "/api/v1/uploads",
            json={"max_edge": max_edge, "files": [{"name": name, "bytes": path.stat().st_size} for name, path in batch.items()]},
        )
        _fail(response, "Upload")
        body = response.json()
        for item in body["rejected"]:
            print(f"Skipped {item['name']}: {item['reason']}")
        for slot in body["uploads"]:
            data = batch[slot["name"]].read_bytes()
            headers = {"Content-Type": slot["content_type"]}
            if slot["upload_url"].startswith("/"):
                sent = client.put(slot["upload_url"], content=data, headers=headers)
            else:
                # A signed storage URL: no sign-in header.
                sent = httpx.put(slot["upload_url"], content=data, headers=headers, timeout=client.timeout)
            if sent.status_code >= 400:
                print(f"Skipped {slot['name']}: upload failed ({sent.status_code})")
                continue
            done = client.post(f"/api/v1/uploads/{slot['upload_id']}/complete")
            if done.status_code >= 400:
                print(f"Skipped {slot['name']}: {done.json()['error']['message']}")
                continue
            accepted.append(done.json())
    return accepted


def cmd_submit(args) -> None:
    folder = args.images.resolve(strict=True)
    files = _image_files(folder)
    if not files:
        sys.exit(f"No files in {folder}")
    settings = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings else {}
    max_edge = settings.get("preprocessing", {}).get("max_edge")
    with _client(args, 120) as client:
        accepted = upload_folder(client, folder, files, max_edge)
        if not accepted:
            sys.exit("No images were accepted")
        labeled = sum(1 for item in accepted if item["label"])
        print(f"Uploaded {len(accepted)} images ({labeled} labeled)")
        settings.setdefault("run", {})["images"] = [item["upload_id"] for item in accepted]
        response = client.post("/api/v1/jobs", json={"name": args.name, "settings": settings})
        _fail(response, "Submitting the job")
        job = response.json()
        minutes = job["estimated_seconds"] / 60
        print(f"Submitted {job['job_id']}: {job['runs']['total']} runs, about {minutes:.0f} min")


def cmd_status(args) -> None:
    with _client(args, 30) as client:
        if args.job:
            response = client.get(f"/api/v1/jobs/{args.job}")
            _fail(response, "Status")
            _print_json(response.json())
            return
        response = client.get("/api/v1/device/status")
        _fail(response, "Status")
        for device in response.json()["devices"]:
            print(f"Phone: {device['state']}" + (f" (unit {device['unit_id']})" if device.get("unit_id") else ""))
        for job in client.get("/api/v1/jobs").json()["jobs"]:
            runs = job["runs"]
            done = runs["completed"] + runs["error"]
            print(f"{job['job_id']}  {job['status']:<9}  {done}/{runs['total']} runs  {job['name']}")


def cmd_fake_worker(args) -> None:
    from .fake_worker import FakeWorker

    cfg = load_config(args.data)
    key = args.key or _key_path(cfg).read_text(encoding="utf-8").strip()
    with httpx.Client(base_url=args.server, timeout=60, follow_redirects=True) as client:
        worker = FakeWorker(client, key)
        while True:
            count = worker.run_until_idle()
            if count:
                print(f"Ran {count} unit(s)")
            if args.once:
                return
            time.sleep(args.poll_seconds)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="phonebench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, help="data folder (default: server/data, or $PHONEBENCH_DATA)")
    parser.add_argument("--env", type=Path, help="file of KEY=VALUE settings, e.g. for cloud mode")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    device_key = commands.add_parser("device-key", help="create or replace the phone's device key")
    device_key.add_argument("--name", default="phone")
    device_key.set_defaults(func=cmd_device_key)

    init_db = commands.add_parser("init-db", help="cloud: apply supabase/migrations (tables and storage bucket)")
    init_db.set_defaults(func=cmd_init_db)

    members = commands.add_parser("members", help="list, add or remove who may sign in")
    members.add_argument("action", choices=("list", "add", "remove"))
    members.add_argument("emails", nargs="*")
    members.add_argument("--admin", action="store_true", help="with add: may cancel anyone's job")
    members.set_defaults(func=cmd_members)

    for name, func, help_text in (
        ("submit", cmd_submit, "upload a folder of images and submit a job"),
        ("status", cmd_status, "list jobs, or show one"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--server", default=DEFAULT_SERVER)
        command.add_argument("--token", help="sign-in token for a cloud server (or $PHONEBENCH_TOKEN)")
        command.set_defaults(func=func)
        if name == "submit":
            command.add_argument("images", type=Path, help="folder of images; real/, synthetic/ and tampered/ subfolders give labels")
            command.add_argument("--settings", type=Path, help="job settings overriding config/defaults.json")
            command.add_argument("--name", default="job")
        else:
            command.add_argument("job", nargs="?")

    fake = commands.add_parser("fake-worker", help="run jobs with a simulated phone")
    fake.add_argument("--server", default=DEFAULT_SERVER)
    fake.add_argument("--key", help="device key (default: read from the data folder)")
    fake.add_argument("--once", action="store_true", help="stop when the queue is empty")
    fake.add_argument("--poll-seconds", type=float, default=2)
    fake.set_defaults(func=cmd_fake_worker)

    args = parser.parse_args(argv)
    if args.env:
        os.environ.update(read_env_file(args.env))
    if args.command == "members" and args.action != "list" and not args.emails:
        parser.error("members add/remove needs at least one email")
    args.func(args)


if __name__ == "__main__":
    main()
