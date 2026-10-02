"""Command line: `python -m phonebench <command>`.

    serve        run the server on 127.0.0.1
    device-key   create (or replace) the phone's device key
    submit       upload a folder of images and submit a job
    status       list jobs, or show one
    fake-worker  run jobs with a simulated phone
"""

import argparse
import json
from pathlib import Path
import sys
import time

import httpx

from .config import load_config
from .store import Store

DEFAULT_SERVER = "http://127.0.0.1:8000"
UPLOAD_BATCH = 20


def _print_json(value) -> None:
    print(json.dumps(value, indent=2))


def cmd_serve(args) -> None:
    import uvicorn

    from .api import create_app

    cfg = load_config(args.data)
    store = Store(cfg.db_path)
    if not store.has_active_device():
        key = store.create_device("phone", time.time())
        cfg.device_key_path.write_text(key + "\n", encoding="utf-8")
        print(f"Created a device key; it is saved in {cfg.device_key_path}")
    store.close()
    if args.host not in ("127.0.0.1", "localhost"):
        print("Warning: local mode has no sign-in; only listen on 127.0.0.1.", file=sys.stderr)
    uvicorn.run(create_app(cfg), host=args.host, port=args.port, log_level="info")


def cmd_device_key(args) -> None:
    cfg = load_config(args.data)
    store = Store(cfg.db_path)
    key = store.create_device("phone", time.time())
    cfg.device_key_path.write_text(key + "\n", encoding="utf-8")
    print(f"New device key saved in {cfg.device_key_path}; the old key no longer works.")


def _image_files(folder: Path) -> list[Path]:
    return sorted(
        path for path in folder.rglob("*")
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(folder).parts)
    )


def cmd_submit(args) -> None:
    folder = args.images.resolve(strict=True)
    files = _image_files(folder)
    if not files:
        sys.exit(f"No files in {folder}")
    settings = json.loads(args.settings.read_text(encoding="utf-8")) if args.settings else {}
    with httpx.Client(base_url=args.server, timeout=120) as client:
        accepted = []
        for start in range(0, len(files), UPLOAD_BATCH):
            batch = files[start:start + UPLOAD_BATCH]
            handles = [path.open("rb") for path in batch]
            try:
                response = client.post(
                    "/api/v1/uploads",
                    files=[("files", (path.name, handle)) for path, handle in zip(batch, handles)],
                    data={"paths": [path.relative_to(folder).as_posix() for path in batch]},
                )
            finally:
                for handle in handles:
                    handle.close()
            response.raise_for_status()
            body = response.json()
            accepted += body["accepted"]
            for item in body["rejected"]:
                print(f"Skipped {item['name']}: {item['reason']}")
        if not accepted:
            sys.exit("No images were accepted")
        labeled = sum(1 for item in accepted if item["label"])
        print(f"Uploaded {len(accepted)} images ({labeled} labeled)")
        settings.setdefault("run", {})["images"] = [item["upload_id"] for item in accepted]
        response = client.post("/api/v1/jobs", json={"name": args.name, "settings": settings})
        if response.status_code >= 400:
            sys.exit(f"Job rejected: {response.json()['error']['message']}")
        job = response.json()
        minutes = job["estimated_seconds"] / 60
        print(f"Submitted {job['job_id']}: {job['runs']['total']} runs, about {minutes:.0f} min")


def cmd_status(args) -> None:
    with httpx.Client(base_url=args.server, timeout=30) as client:
        if args.job:
            response = client.get(f"/api/v1/jobs/{args.job}")
            response.raise_for_status()
            _print_json(response.json())
            return
        devices = client.get("/api/v1/device/status").json()["devices"]
        for device in devices:
            print(f"Phone: {device['state']}" + (f" (unit {device['unit_id']})" if device.get("unit_id") else ""))
        for job in client.get("/api/v1/jobs").json()["jobs"]:
            runs = job["runs"]
            done = runs["completed"] + runs["error"]
            print(f"{job['job_id']}  {job['status']:<9}  {done}/{runs['total']} runs  {job['name']}")


def cmd_fake_worker(args) -> None:
    from .fake_worker import FakeWorker

    cfg = load_config(args.data)
    key = args.key or cfg.device_key_path.read_text(encoding="utf-8").strip()
    with httpx.Client(base_url=args.server, timeout=60) as client:
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
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="run the server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.set_defaults(func=cmd_serve)

    device_key = commands.add_parser("device-key", help="create or replace the phone's device key")
    device_key.set_defaults(func=cmd_device_key)

    submit = commands.add_parser("submit", help="upload a folder of images and submit a job")
    submit.add_argument("images", type=Path, help="folder of images; real/, synthetic/ and tampered/ subfolders give labels")
    submit.add_argument("--settings", type=Path, help="job settings overriding config/defaults.json")
    submit.add_argument("--name", default="job")
    submit.add_argument("--server", default=DEFAULT_SERVER)
    submit.set_defaults(func=cmd_submit)

    status = commands.add_parser("status", help="list jobs, or show one")
    status.add_argument("job", nargs="?")
    status.add_argument("--server", default=DEFAULT_SERVER)
    status.set_defaults(func=cmd_status)

    fake = commands.add_parser("fake-worker", help="run jobs with a simulated phone")
    fake.add_argument("--server", default=DEFAULT_SERVER)
    fake.add_argument("--key", help="device key (default: read from the data folder)")
    fake.add_argument("--once", action="store_true", help="stop when the queue is empty")
    fake.add_argument("--poll-seconds", type=float, default=2)
    fake.set_defaults(func=cmd_fake_worker)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
