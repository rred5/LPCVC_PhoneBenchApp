"""Run warm-context repetitions using settings from benchmark_defaults.json."""
import argparse
import base64
from datetime import datetime, timezone
import json
import re
import time

from run_benchmark_matrix import add_common_arguments, adb, battery_celsius, resolved_arguments, summarize


def parse_result(app_result, metrics, save_response):
    if app_result.get("status") != "completed":
        raise RuntimeError(app_result.get("error") or "App reported a failed inference")
    values = {}
    for key in metrics:
        if key == "reported_total_tokens":
            values[key] = app_result["prompt_tokens"] + app_result["generated_tokens"]
        elif app_result.get(key) is None:
            raise RuntimeError(f"Missing metric: {key}")
        else:
            values[key] = app_result[key]
    if save_response:
        values["response"] = app_result.get("response", "")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_common_arguments(parser)
    args = parser.parse_args()
    defaults = resolved_arguments(args, parser)
    if not re.fullmatch(r"[A-Za-z0-9_.-]+\.png", args.image):
        parser.error("--image must be a PNG filename")
    if args.repetitions < 1 or args.rest_seconds < 0:
        parser.error("Invalid repetitions or rest duration")
    backends = [backend.upper() for backend in args.backends]
    if not backends or len(backends) != len(set(backends)) or any(backend not in {"CPU", "GPU", "NPU"} for backend in backends):
        parser.error("--backends must be a unique subset of CPU GPU NPU")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    settings = {"mode": "warm-context-reset", "image": args.image, "model": args.model, "prompt": args.prompt,
                "model_already_downloaded": args.model_already_downloaded, "repetitions": args.repetitions,
                "backends": backends, "rest_seconds": args.rest_seconds,
                "output": defaults["output"]}
    settings_path = output / "run-settings.json"
    if settings_path.exists() and json.loads(settings_path.read_text(encoding="utf-8")) != settings:
        parser.error("Existing output settings differ; choose a new --output directory")
    settings_path.write_text(json.dumps(settings, indent=2), encoding="utf-8")

    sequence = 0
    for backend in backends:
        complete = all((output / f"{backend}-run-{run:02d}.json").exists() for run in range(1, args.repetitions + 1))
        if complete:
            sequence += args.repetitions
            print(f"Already recorded {backend}", flush=True)
            continue
        print(f"START warm {backend}: {args.repetitions} measured runs plus warm-up", flush=True)
        # One test session keeps this backend's model loaded.
        start = time.monotonic()
        before = battery_celsius()
        command = adb("shell", "am", "instrument", "-w", "-r", "-e", "backend", backend,
                      "-e", "repetitions", str(args.repetitions), "-e", "restMs", str(round(args.rest_seconds * 1000)),
                      "-e", "image", args.image, "-e", "modelBase64", base64.b64encode(args.model.encode()).decode(),
                      "-e", "downloadModel", str(not args.model_already_downloaded).lower(),
                      "-e", "promptBase64", base64.b64encode(args.prompt.encode()).decode(), "-e", "class",
                      "com.geniex.demo.GenieXWarmBenchmarkTest#testWarmBackend",
                      "com.geniex.demo.test/androidx.test.runner.AndroidJUnitRunner", timeout=1800)
        if defaults["output"]["save_instrumentation_log"]:
            (output / f"{backend}-instrumentation.txt").write_text(command.stdout + command.stderr, encoding="utf-8")
        if command.returncode != 0 or "OK (1 test)" not in command.stdout:
            raise RuntimeError(f"Warm {backend} test failed")
        after = battery_celsius()
        for run in range(1, args.repetitions + 1):
            sequence += 1
            result = adb("shell", "run-as", "com.geniex.demo", "cat", f"files/benchmark/results/warm-{backend}-{run}.json")
            if result.returncode:
                raise RuntimeError(result.stderr)
            app_result = json.loads(result.stdout)
            if defaults["output"]["save_raw_result"]:
                (output / f"{backend}-run-{run:02d}-result.json").write_text(json.dumps(app_result, indent=2), encoding="utf-8")
            row = {"sequence": sequence, "backend": backend, "run": run, "mode": "warm-context-reset",
                   "status": "passed", "started_utc": datetime.now(timezone.utc).isoformat(),
                   "battery_c_before": before, "battery_c_after": after,
                   # Add per-run battery sampling here for thermal studies.
                   "elapsed_s": round((time.monotonic() - start) / args.repetitions, 2)}
            row.update(parse_result(app_result, defaults["output"]["metrics"], defaults["output"]["save_response"]))
            (output / f"{backend}-run-{run:02d}.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
        summarize(output, backends, defaults["output"])
        print(f"DONE warm {backend}", flush=True)
    print(json.dumps(summarize(output, backends, defaults["output"]), indent=2), flush=True)


if __name__ == "__main__":
    main()
