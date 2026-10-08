"""Run a two-stage tamper series on the phone: one model load, many images.

Each image is one conversation. Stage 1 sends the image and the first prompt; stage 2 sends
the follow-up in the same conversation (no reset, no second image). Both stages always run.
The model is loaded once and every result is pulled as it appears, together with battery
temperature, so the run shows how inference time drifts as the phone warms up.

Output folder:
    run-settings.json    what was run, and the phone it ran on
    series.json          what was pushed to the phone
    results/             raw app result per stage (s0001-1.json, s0001-2.json, ...)
    iterations.jsonl     one line per image: both stage results and temperatures
    thermal.jsonl        every temperature sample with seconds since the run started
    instrumentation.txt  the test runner's log
    summary.json         counts, host wall time and the test's exit status

Score and plot it with analyze_two_stage.py.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

from run_benchmark_matrix import adb
from tamper_dataset import load_manifest, select_records
from two_stage import load_prompts, render


PACKAGE = "com.geniex.demo"
REMOTE_BENCHMARK = "files/benchmark"
RESULT_NAME = re.compile(r"^s(\d{4})-([12])\.json$")
REPO_ROOT = Path(__file__).resolve().parent
MODEL_LIST = REPO_ROOT / "src" / "main" / "assets" / "model_list.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def catalog_entry(display_name: str) -> dict | None:
    """The model's entry in the app's bundled catalog, to fail early on a mistyped name."""
    entries = json.loads(MODEL_LIST.read_text(encoding="utf-8"))
    entries = entries if isinstance(entries, list) else entries.get("models", [])
    return next((entry for entry in entries if entry.get("displayName") == display_name), None)


def build_series(records: list[dict], prompts: dict, model: str, backend: str,
                 rest_seconds: float, max_tokens: tuple[int, int]) -> dict:
    """The file the on-phone test reads. Stage 2 is rendered per image with its pushed size."""
    return {
        "schema_version": 1, "model": model, "backend": backend.upper(),
        "rest_ms": round(rest_seconds * 1000),
        "max_tokens": {"stage1": max_tokens[0], "stage2": max_tokens[1]},
        "stage1": prompts["stage1"],
        "items": [
            {"index": number, "image": record["remote_filename"],
             "stage2": render(prompts["stage2"], *record["prepared_size"])}
            for number, record in enumerate(records, start=1)
        ],
    }


def push_file(local: Path, remote_name: str, run=adb) -> bool:
    """Copy a file into the app's private benchmark folder, verified by SHA-256.

    Returns False when the phone already had identical bytes.
    """
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", remote_name):
        raise ValueError(f"Unsafe remote filename: {remote_name}")
    remote = f"{REMOTE_BENCHMARK}/{remote_name}"
    local_hash = sha256_file(local)
    present = run("shell", "run-as", PACKAGE, "sha256sum", remote)
    if present.returncode == 0 and present.stdout.split()[:1] == [local_hash]:
        return False
    staging = f"/data/local/tmp/geniex-{uuid.uuid4().hex}-{remote_name}"
    if run("shell", "run-as", PACKAGE, "mkdir", "-p", REMOTE_BENCHMARK).returncode:
        raise RuntimeError("Cannot reach the app's private files; install the debug app first")
    try:
        pushed = run("push", str(local), staging, timeout=300)
        if pushed.returncode:
            raise RuntimeError(f"adb push failed for {local.name}: {pushed.stderr.strip()}")
        copied = run("shell", "run-as", PACKAGE, "cp", staging, remote)
        if copied.returncode:
            raise RuntimeError(f"Copy into the app failed for {remote_name}: {copied.stderr.strip()}")
        check = run("shell", "run-as", PACKAGE, "sha256sum", remote)
        if check.stdout.split()[:1] != [local_hash]:
            raise RuntimeError(f"Checksum mismatch after pushing {remote_name}")
    finally:
        run("shell", "rm", "-f", staging)
    return True


def clear_results(run=adb) -> None:
    """Delete result files left by an earlier run, and make sure they are gone.

    `adb shell` joins its arguments with spaces before the phone's shell parses them, so the script
    for `sh -c` has to arrive as one quoted argument; otherwise `sh -c` runs a bare `rm` and deletes
    nothing, and the old run's results get read as this run's.
    """
    results = f"{REMOTE_BENCHMARK}/results"
    run("shell", "run-as", PACKAGE, "sh", "-c", f"'rm -f {results}/s*.json {results}/warmup-*.json'")
    listing = run("shell", "run-as", PACKAGE, "ls", results)
    left = [name for name in listing.stdout.split() if RESULT_NAME.match(name) or name.startswith("warmup-")]
    if left:
        raise RuntimeError(f"Could not clear earlier results from the phone ({', '.join(left[:4])}...); "
                           "the run would mix in another run's numbers")


def thermal_sample(run=adb) -> dict:
    """Battery temperature (deg C) and Android's thermal status (0 none ... 6 shutdown), when readable."""
    celsius = status = None
    try:
        match = re.search(r"temperature:\s*(\d+)", run("shell", "dumpsys", "battery").stdout)
        celsius = int(match[1]) / 10 if match else None
        match = re.search(r"Thermal Status:\s*(\d+)", run("shell", "dumpsys", "thermalservice").stdout)
        status = int(match[1]) if match else None
    except (subprocess.SubprocessError, OSError):
        pass
    return {"battery_c": celsius, "thermal_status": status}


class Collector:
    """Pulls result files as the phone writes them and joins the two stages of each image."""

    def __init__(self, output: Path, records: list[dict], run=adb, clock=time.monotonic, expected_model: str | None = None):
        self.output, self.records, self.run, self.clock = output, records, run, clock
        self.expected_model = expected_model
        self.started = clock()
        self.stages: dict[tuple[int, int], dict] = {}
        self.finished: set[int] = set()
        self.last_battery = None
        self.last_finish_battery = None
        (output / "results").mkdir(parents=True, exist_ok=True)

    def adopt(self, finished_indices: set[int]) -> None:
        self.finished |= finished_indices

    def _remote_names(self) -> list[str]:
        listing = self.run("shell", "run-as", PACKAGE, "ls", f"{REMOTE_BENCHMARK}/results")
        return listing.stdout.split() if listing.returncode == 0 else []

    def sample_temperature(self) -> None:
        sample = thermal_sample(self.run)
        self.last_battery = sample["battery_c"]
        line = {"t_s": round(self.clock() - self.started, 2), **sample}
        with (self.output / "thermal.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(line) + "\n")

    def poll(self, final: bool = False) -> int:
        """Pull new results; return how many images finished during this poll."""
        self.sample_temperature()
        for name in self._remote_names():
            match = RESULT_NAME.match(name)
            if not match:
                continue
            key = (int(match[1]), int(match[2]))
            if key in self.stages or key[0] in self.finished:
                continue
            fetched = self.run("shell", "run-as", PACKAGE, "cat", f"{REMOTE_BENCHMARK}/results/{name}")
            try:
                result = json.loads(fetched.stdout)
            except ValueError:
                continue  # still being written; the next poll will get it
            if self.expected_model is not None and result.get("model") != self.expected_model:
                raise RuntimeError(f"{name} is from model {result.get('model')!r}, not {self.expected_model!r}: "
                                   "it is left over from another run")
            result["_seen_s"] = round(self.clock() - self.started, 2)
            self.stages[key] = result
            (self.output / "results" / name).write_text(json.dumps(result, indent=2), encoding="utf-8")
        done = 0
        for index in sorted({index for index, _stage in self.stages} - self.finished):
            first, second = self.stages.get((index, 1)), self.stages.get((index, 2))
            if second is not None or (first is not None and first.get("status") != "completed") or (final and first is not None):
                self._finish(index, first, second)
                done += 1
        return done

    def _finish(self, index: int, first: dict | None, second: dict | None) -> None:
        record = self.records[index - 1]
        row = {
            "iteration": index, "record_id": record["remote_filename"], "pair_id": record["pair_id"],
            "variant": record["variant"], "stage1": first, "stage2": second,
            "battery_c_before": self.last_finish_battery, "battery_c_after": self.last_battery,
            "seen_s": (second or first or {}).get("_seen_s"),
        }
        self.last_finish_battery = self.last_battery
        with (self.output / "iterations.jsonl").open("a", encoding="utf-8") as file:
            file.write(json.dumps(row) + "\n")
        self.finished.add(index)


def load_finished(output: Path) -> set[int]:
    path = output / "iterations.jsonl"
    if not path.is_file():
        return set()
    return {json.loads(line)["iteration"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()}


def device_facts() -> dict:
    def prop(name: str) -> str | None:
        result = adb("shell", "getprop", name)
        return result.stdout.strip() or None if result.returncode == 0 else None
    return {"model": prop("ro.product.model"), "soc": prop("ro.soc.model"),
            "android": prop("ro.build.version.release"), "sdk": prop("ro.build.version.sdk")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True, help="manifest.json from tamper_dataset.py")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True, help="Display name in the app's catalog, e.g. 'Qwen3-VL-2B-Instruct (GGUF)'")
    parser.add_argument("--backend", default="NPU", choices=["CPU", "GPU", "NPU"])
    parser.add_argument("--label", help="Name for this run in reports (default: <runtime>-<backend>)")
    parser.add_argument("--count", type=int, default=50, help="Images to run, as whole real/tampered pairs (even)")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--prompts", type=Path, default=REPO_ROOT / "two_stage_prompts.json")
    parser.add_argument("--rest-seconds", type=float, default=5.0)
    parser.add_argument("--max-tokens-stage1", type=int, default=256)
    parser.add_argument("--max-tokens-stage2", type=int, default=128)
    parser.add_argument("--model-already-downloaded", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--poll-seconds", type=float, default=10.0)
    parser.add_argument("--timeout-hours", type=float, default=8.0)
    parser.add_argument("--dry-run", action="store_true", help="Write series.json and print the plan without touching the phone")
    args = parser.parse_args()
    if min(args.max_tokens_stage1, args.max_tokens_stage2) < 1 or args.rest_seconds < 0 or args.poll_seconds <= 0:
        parser.error("Token caps must be positive; rest must be non-negative; poll interval must be positive")

    entry = catalog_entry(args.model)
    if entry is None:
        names = [e["displayName"] for e in json.loads(MODEL_LIST.read_text(encoding="utf-8")) if e.get("type") == "vlm"]
        parser.error(f"'{args.model}' is not in the app's catalog. Vision models: {names}")
    if entry.get("type") != "vlm":
        parser.error(f"'{args.model}' is not a vision model")
    runtime = entry.get("runtime", "unknown")
    manifest = load_manifest(args.manifest)
    try:
        records = select_records(manifest, args.count, args.seed)
    except ValueError as error:
        parser.error(str(error))
    prompts = load_prompts(args.prompts)
    series = build_series(records, prompts, args.model, args.backend, args.rest_seconds,
                          (args.max_tokens_stage1, args.max_tokens_stage2))
    settings = {
        "label": args.label or f"{runtime}-{args.backend.lower()}", "model": args.model, "runtime": runtime,
        "backend": args.backend, "count": args.count, "seed": args.seed, "manifest": str(args.manifest.resolve()),
        "prompts_sha256": hashlib.sha256(args.prompts.read_bytes()).hexdigest(),
        "rest_seconds": args.rest_seconds, "max_tokens": series["max_tokens"],
        "mode": "two-stage-same-conversation",
    }
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    settings_path = output / "run-settings.json"
    if settings_path.exists():
        existing = json.loads(settings_path.read_text(encoding="utf-8"))
        changed = sorted(key for key, value in settings.items() if key in existing and existing[key] != value)
        if changed:
            parser.error(f"Existing output was made with different {', '.join(changed)}; choose a new --output folder")
    seconds_estimate = len(records) * (50 + args.rest_seconds)
    print(f"{len(records)} images ({sum(r['variant'] == 'tampered' for r in records)} tampered) on {args.model} [{runtime}, {args.backend}]; "
          f"roughly {seconds_estimate / 60:.0f} minutes at last year's ~50 s per image")
    (output / "series.json").write_text(json.dumps(series, indent=2), encoding="utf-8")
    if args.dry_run:
        settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
        print(f"Dry run: wrote {output / 'series.json'}")
        return 0

    devices = [line for line in adb("devices").stdout.splitlines()[1:] if line.strip().endswith("device")]
    if len(devices) != 1:
        parser.error(f"Expected exactly one connected phone, found {len(devices)}")
    settings["device"] = device_facts()
    settings["started_utc"] = datetime.now(timezone.utc).isoformat()
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    packages = adb("shell", "pm", "list", "packages").stdout
    if f"package:{PACKAGE}" not in packages or f"package:{PACKAGE}.test" not in packages:
        parser.error("Install the debug app and its test APK first (run_benchmark.ps1 -BuildAndInstall)")

    finished = load_finished(output)
    start = max(finished, default=0) + 1
    if start > len(records):
        print("Every image in this series is already recorded.")
        return 0
    print(f"Pushing {len(records)} images...", flush=True)
    pushed = sum(push_file(manifest_path_for(args.manifest, r), r["remote_filename"]) for r in records)
    push_file(output / "series.json", "series.json")
    print(f"Pushed {pushed} new images; starting at image {start}.", flush=True)
    clear_results()

    collector = Collector(output, records, expected_model=args.model)
    collector.adopt(finished)
    command = ["adb", "shell", "am", "instrument", "-w", "-r", "-e", "series", "series.json", "-e", "start", str(start),
               "-e", "downloadModel", str(not args.model_already_downloaded).lower(),
               "-e", "class", f"{PACKAGE}.GenieXTwoStageSeriesTest#testSeries", f"{PACKAGE}.test/androidx.test.runner.AndroidJUnitRunner"]
    started = time.monotonic()
    deadline = started + args.timeout_hours * 3600
    with (output / "instrumentation.txt").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            while process.poll() is None:
                if time.monotonic() > deadline:
                    print("Time limit reached; stopping the run.", flush=True)
                    break
                if collector.poll():
                    print(f"{len(collector.finished)}/{len(records)} images, battery {collector.last_battery} C", flush=True)
                time.sleep(args.poll_seconds)
        except KeyboardInterrupt:
            print("Interrupted; rerun the same command to resume.", flush=True)
        if process.poll() is None:
            # Ending the host-side adb command does not stop the test on the phone.
            process.kill()
            adb("shell", "am", "force-stop", PACKAGE)
    collector.poll(final=True)
    log_text = (output / "instrumentation.txt").read_text(encoding="utf-8", errors="replace")
    summary = {
        "images_recorded": len(collector.finished), "images_requested": len(records),
        "host_total_s": round(time.monotonic() - started, 1), "instrument_exit": process.returncode,
        "instrumentation_ok": "OK (1 test)" in log_text,
    }
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["instrumentation_ok"] and len(collector.finished) == len(records) else 1


def manifest_path_for(manifest_path: Path, record: dict) -> Path:
    return manifest_path.resolve().parent / record["prepared"]


if __name__ == "__main__":
    sys.exit(main())
