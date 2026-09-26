"""Run a cold benchmark matrix using settings from benchmark_defaults.json."""
import argparse
import base64
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics
import subprocess
import time


METRICS = ("prompt_tokens", "generated_tokens", "reported_total_tokens", "ttft_ms", "prefill_tok_s", "decode_tok_s")


def adb(*args, timeout=30):
    return subprocess.run(["adb", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def battery_celsius():
    match = re.search(r"temperature:\s*(\d+)", adb("shell", "dumpsys", "battery").stdout)
    return int(match[1]) / 10 if match else None


def load_defaults(path):
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
        run, preprocessing, output = settings["run"], settings["preprocessing"], settings["output"]
    except (OSError, KeyError, json.JSONDecodeError) as error:
        raise ValueError(f"Invalid settings file {path}: {error}") from error
    if settings.get("schema_version") != 1:
        raise ValueError("Only settings schema_version 1 is supported")
    required_run = ("mode", "model", "model_already_downloaded", "prompt", "repetitions", "backends", "rest_seconds")
    if any(key not in run for key in required_run) or "max_edge" not in preprocessing:
        raise ValueError("Settings file is missing a required run or preprocessing field")
    required_output = ("save_response", "save_raw_result", "save_instrumentation_log", "save_csv", "save_summary", "metrics")
    if any(key not in output for key in required_output):
        raise ValueError("Settings file is missing a required output field")
    if not set(output["metrics"]).issubset(METRICS) or len(output["metrics"]) != len(set(output["metrics"])):
        raise ValueError(f"output.metrics must be a unique subset of: {', '.join(METRICS)}")
    return settings


def resolved_arguments(args, parser):
    try:
        defaults = load_defaults(args.settings)
    except ValueError as error:
        parser.error(str(error))
    run = defaults["run"]
    args.image = args.image or "L106-768.png"
    args.model = args.model or run["model"]
    args.model_already_downloaded = args.model_already_downloaded if args.model_already_downloaded is not None else run["model_already_downloaded"]
    args.prompt = args.prompt or run["prompt"]
    args.repetitions = args.repetitions if args.repetitions is not None else run["repetitions"]
    args.backends = args.backends or run["backends"]
    args.rest_seconds = args.rest_seconds if args.rest_seconds is not None else run["rest_seconds"]
    return defaults


def summarize(folder, backends, output):
    # Raw app records share this prefix; only normalized rows belong in summaries.
    rows = [json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(folder.glob("*-run-*.json"))
            if re.fullmatch(r"(?:CPU|GPU|NPU)-run-\d+\.json", path.name)]
    columns = ["sequence", "backend", "run", "status", *output["metrics"],
               "battery_c_before", "battery_c_after", "elapsed_s", "started_utc"]
    if output["save_response"]:
        columns.append("response")
    columns.append("error")
    if output["save_csv"]:
        with (folder / "runs.csv").open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(sorted(rows, key=lambda row: row["sequence"]))
    summaries = []
    for backend in backends:
        cases = [row for row in rows if row["backend"] == backend]
        passed = [row for row in cases if row["status"] == "passed"]
        summary = {"backend": backend, "attempted": len(cases), "passed": len(passed),
                   "failed": len(cases) - len(passed)}
        for key in output["metrics"]:
            values = [row[key] for row in passed]
            summary[f"mean_{key}"] = statistics.mean(values) if values else None
            summary[f"sd_{key}"] = statistics.stdev(values) if len(values) > 1 else None
        summaries.append(summary)
    if output["save_summary"]:
        (folder / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    return summaries


def add_common_arguments(parser):
    parser.add_argument("--settings", type=Path, default=Path(__file__).with_name("benchmark_defaults.json"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--image", help="PNG filename already in GenieX files/benchmark")
    parser.add_argument("--model")
    parser.add_argument("--model-already-downloaded", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--prompt")
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--backends", nargs="+")
    parser.add_argument("--rest-seconds", type=float)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    args = parser.parse_args()
    defaults = resolved_arguments(args, parser)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.png", args.image):
        parser.error("--image must be a PNG filename, not a path")
    if args.repetitions < 1 or args.rest_seconds < 0:
        parser.error("--repetitions must be positive and --rest-seconds non-negative")
    backends = [backend.upper() for backend in args.backends]
    if not backends or len(backends) != len(set(backends)) or any(backend not in {"CPU", "GPU", "NPU"} for backend in backends):
        parser.error("--backends must be a unique subset of CPU GPU NPU")

    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    settings = {"mode": "cold", "image": args.image, "model": args.model, "prompt": args.prompt,
                "model_already_downloaded": args.model_already_downloaded, "repetitions": args.repetitions,
                "backends": backends, "rest_seconds": args.rest_seconds,
                "output": defaults["output"]}
    settings_path = folder / "run-settings.json"
    if settings_path.exists() and json.loads(settings_path.read_text(encoding="utf-8")) != settings:
        parser.error("Existing output settings differ; choose a new --output directory")
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    scheduled = []
    sequence = 0
    for run in range(1, args.repetitions + 1):
        # Rotate order to reduce backend-order bias across repetitions.
        offset = (run - 1) % len(backends)
        for backend in backends[offset:] + backends[:offset]:
            sequence += 1
            stem = f"{backend}-run-{run:02d}"
            saved = folder / f"{stem}.json"
            if saved.exists():
                print(f"Already recorded {stem}", flush=True)
                continue
            scheduled.append((sequence, backend, run, stem, saved))
    if not scheduled:
        print(json.dumps(summarize(folder, backends, defaults["output"]), indent=2), flush=True)
        return

    print(f"START cold cycle: {len(scheduled)} cases in one app session", flush=True)
    started = time.monotonic()
    before = battery_celsius()
    cases = ",".join(f"{backend}:{run}" for _, backend, run, _, _ in scheduled)
    instrument = adb("shell", "am", "instrument", "-w", "-r", "-e", "cases", cases,
        "-e", "restMs", str(round(args.rest_seconds * 1000)), "-e", "image", args.image,
        "-e", "downloadModel", str(not args.model_already_downloaded).lower(),
        "-e", "modelBase64", base64.b64encode(args.model.encode()).decode(),
        "-e", "promptBase64", base64.b64encode(args.prompt.encode()).decode(),
        "-e", "class", "com.geniex.demo.GenieXBenchmarkTest#testL106",
        "com.geniex.demo.test/androidx.test.runner.AndroidJUnitRunner", timeout=1800)
    if defaults["output"]["save_instrumentation_log"]:
        (folder / "cold-instrumentation.txt").write_text(instrument.stdout + instrument.stderr, encoding="utf-8")
    after = battery_celsius()
    for sequence, backend, run, stem, saved in scheduled:
        row = {"sequence": sequence, "backend": backend, "run": run, "status": "failed",
               "started_utc": datetime.now(timezone.utc).isoformat(), "battery_c_before": before,
               "battery_c_after": after, "elapsed_s": round((time.monotonic() - started) / len(scheduled), 2)}
        try:
            result = adb("shell", "run-as", "com.geniex.demo", "cat", f"files/benchmark/results/cold-{backend}-{run}.json")
            if result.returncode:
                raise RuntimeError("No result record; inspect cold-instrumentation.txt")
            app_result = json.loads(result.stdout)
            if app_result.get("status") != "completed":
                raise RuntimeError(app_result.get("error") or "App reported a failed inference")
            if defaults["output"]["save_raw_result"]:
                (folder / f"{stem}-result.json").write_text(json.dumps(app_result, indent=2), encoding="utf-8")
            for key in defaults["output"]["metrics"]:
                row[key] = app_result["prompt_tokens"] + app_result["generated_tokens"] if key == "reported_total_tokens" else app_result[key]
            if defaults["output"]["save_response"]:
                row["response"] = app_result.get("response", "")
            row["status"] = "passed"
        except Exception as error:
            row["error"] = str(error)
        saved.write_text(json.dumps(row, indent=2), encoding="utf-8")
        summarize(folder, backends, defaults["output"])
        print(f"DONE {sequence} {stem}: {row['status']}", flush=True)
    print(json.dumps(summarize(folder, backends, defaults["output"]), indent=2), flush=True)


if __name__ == "__main__":
    main()
