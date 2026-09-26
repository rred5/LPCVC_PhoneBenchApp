"""Run a small NPU accuracy pilot and write resumable reports.

Each selected image gets its own checkpoint directory. The Android warm test
does one excluded warm-up and then records the requested measured repetitions.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time

from prepare_accuracy_dataset import build_manifest


LABELS = {"real", "synthetic", "tampered"}
BINARY_OF = {"real": "real", "synthetic": "manipulated", "tampered": "manipulated"}
CUE_LABELS = {
    "real": "real", "genuine": "real", "authentic": "real",
    "unaltered": "real", "untampered": "real", "unmodified": "real",
    "synthetic": "synthetic", "ai-generated": "synthetic", "ai generated": "synthetic",
    "computer-generated": "synthetic", "cgi": "synthetic", "fake": "synthetic",
    "fabricated": "synthetic",
    "tampered": "tampered", "tampering": "tampered", "manipulated": "tampered",
    "manipulation": "tampered", "edited": "tampered", "altered": "tampered",
    "doctored": "tampered", "photoshopped": "tampered", "composited": "tampered",
}
CUE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(cue) for cue in sorted(CUE_LABELS, key=len, reverse=True)) + r")\b"
)
NEGATION_PATTERN = re.compile(
    r"\b(?:not|no|never|without|cannot|can't|nothing|free of|lacks|lacking|absent|n't)\b"
)
CLAUSE_SPLIT = re.compile(r"[,;:]|\bbut\b|\bhowever\b|\balthough\b|\bwhereas\b")
METRICS = (
    "prompt_tokens",
    "generated_tokens",
    "reported_total_tokens",
    "ttft_ms",
    "prefill_tok_s",
    "decode_tok_s",
)
REPO_ROOT = Path(__file__).resolve().parent
PUSH_SCRIPT = REPO_ROOT / "push_benchmark_image.ps1"
WARM_RUNNER = REPO_ROOT / "run_warm_benchmark.py"


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_settings(path: Path) -> dict:
    try:
        settings = read_json(path)
        run = settings["run"]
        settings["preprocessing"]["max_edge"]
        output = settings["output"]
        if settings.get("schema_version") != 1:
            raise ValueError("only settings schema_version 1 is supported")
        if not isinstance(run.get("pairs"), list) or not run["pairs"]:
            raise ValueError("run.pairs must be a non-empty list")
        if not isinstance(run.get("prompt"), str) or not run["prompt"].strip():
            raise ValueError("run.prompt must be non-empty")
        if not output.get("save_response") or not output.get("save_raw_result"):
            raise ValueError("accuracy pilot requires save_response and save_raw_result")
        if any(metric not in METRICS for metric in output.get("metrics", [])):
            raise ValueError("output.metrics contains an unsupported metric")
        return settings
    except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError(f"Invalid pilot settings {path}: {error}") from error


def parse_pairs(value: str, available: list[str]) -> list[str]:
    if value.isdigit():
        count = int(value)
        if count < 1:
            raise ValueError("pair count must be positive")
        selected = available[:count]
    else:
        selected = [part.strip() for part in value.split(",") if part.strip()]
    missing = [pair for pair in selected if pair not in available]
    if missing:
        raise ValueError(f"Unknown pair(s): {', '.join(missing)}")
    if not selected:
        raise ValueError("At least one pair is required")
    return selected


def strict_classification(response: str) -> str | None:
    """Accept only a bare object with the exact classification key/value."""
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(response.strip(), object_pairs_hook=reject_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"classification"}:
        return None
    label = value["classification"]
    return label if isinstance(label, str) and label in LABELS else None


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def _sentence_verdict(sentence: str) -> str | None:
    for match in CUE_PATTERN.finditer(sentence):
        label = CUE_LABELS[match.group(1)]
        clause = CLAUSE_SPLIT.split(sentence[: match.start()])[-1]
        if NEGATION_PATTERN.search(clause):
            # "no signs of manipulation" asserts the opposite of its cue.
            if label == "real":
                continue
            return "real"
        return label
    return None


def lenient_classification(response: str) -> str | None:
    """Label a freeform response, tolerating casing, phrasing, and negation.

    A verdict in the opening sentence wins. Otherwise the model reasoned before
    committing, so the closing verdict is its conclusion rather than a premise.
    """
    if not isinstance(response, str):
        return None
    text = response.replace("’", "'").replace("“", '"').replace("”", '"').strip()
    if not text:
        return None
    if text.strip(" \t*`\"'.!").lower() in LABELS:
        return text.strip(" \t*`\"'.!").lower()
    strict = strict_classification(response)
    if strict is not None:
        return strict
    sentences = _sentences(text.lower())
    if not sentences:
        return None
    opening = _sentence_verdict(sentences[0])
    if opening is not None:
        return opening
    for sentence in reversed(sentences[1:]):
        verdict = _sentence_verdict(sentence)
        if verdict is not None:
            return verdict
    return None


def binary_fields(diagnostic: str | None, expected: str) -> dict:
    """Score the lenient label three ways: raw, three-class, and real vs manipulated."""
    predicted_binary = BINARY_OF.get(diagnostic) if diagnostic is not None else None
    expected_binary = BINARY_OF[expected]
    return {
        "diagnostic_label": diagnostic,
        "diagnostic_valid": diagnostic is not None,
        "diagnostic_correct": diagnostic == expected if diagnostic is not None else False,
        "binary_label": predicted_binary,
        "binary_expected": expected_binary,
        "binary_correct": predicted_binary == expected_binary if predicted_binary is not None else False,
    }


def run_command(command: list[str], timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def clear_remote_results(repetitions: int) -> None:
    paths = [f"files/benchmark/results/warm-NPU-{run}.json" for run in range(1, repetitions + 1)]
    result = run_command(["adb", "shell", "run-as", "com.geniex.demo", "rm", "-f", *paths])
    if result.returncode:
        raise RuntimeError(f"Could not clear phone result checkpoints: {result.stderr.strip()}")


def push_image(image: Path) -> str:
    result = run_command([
        "powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PUSH_SCRIPT),
        "-ImagePath", str(image),
    ], timeout=300)
    if result.returncode:
        raise RuntimeError(f"Image transfer failed for {image.name}: {result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def run_image(
    record: dict,
    image_dir: Path,
    args: argparse.Namespace,
) -> dict:
    image_dir.mkdir(parents=True, exist_ok=True)
    prepared = args.dataset / record["prepared"]
    if not prepared.is_file():
        raise FileNotFoundError(prepared)
    if sha256(prepared) != record["prepared_sha256"]:
        raise RuntimeError(f"Prepared image hash changed: {prepared}")

    image_settings = {
        "pair_id": record["pair_id"],
        "difficulty": record["difficulty"],
        "expected_label": record["expected_label"],
        "source": record["source"],
        "source_sha256": record["source_sha256"],
        "prepared": record["prepared"],
        "prepared_sha256": record["prepared_sha256"],
        "prepared_size": record["prepared_size"],
        "remote_filename": record["remote_filename"],
        "model": args.model,
        "prompt": args.prompt,
        "backend": "NPU",
        "repetitions": args.repetitions,
        "mode": "warm-context-reset",
        "model_already_downloaded": args.model_already_downloaded,
    }
    settings_path = image_dir / "image-settings.json"
    if settings_path.exists() and read_json(settings_path) != image_settings:
        raise RuntimeError(f"Checkpoint settings differ for {image_dir}; choose a new output folder")
    write_json(settings_path, image_settings)

    completed = image_dir / "checkpoint-complete.json"
    if completed.exists():
        return read_json(completed)

    clear_remote_results(args.repetitions)
    transfer_log = push_image(prepared)
    (image_dir / "transfer.txt").write_text(transfer_log, encoding="utf-8")

    runs_dir = image_dir / "runs"
    command = [
        sys.executable, str(WARM_RUNNER),
        "--settings", str(args.settings),
        "--output", str(runs_dir),
        "--image", record["remote_filename"],
        "--model", args.model,
        "--model-already-downloaded" if args.model_already_downloaded else "--no-model-already-downloaded",
        "--prompt", args.prompt,
        "--repetitions", str(args.repetitions),
        "--backends", "NPU",
        "--rest-seconds", str(args.rest_seconds),
    ]
    started = time.monotonic()
    process = run_command(command, timeout=args.timeout_seconds)
    (image_dir / "pilot-runner.txt").write_text(process.stdout + process.stderr, encoding="utf-8")
    rows = []
    for run in range(1, args.repetitions + 1):
        row_path = runs_dir / f"NPU-run-{run:02d}.json"
        raw_path = runs_dir / f"NPU-run-{run:02d}-result.json"
        if not row_path.is_file() or not raw_path.is_file():
            continue
        row = read_json(row_path)
        raw = read_json(raw_path)
        for field, expected in {
            "backend": "NPU", "model": args.model, "image": record["remote_filename"],
            "prompt": args.prompt, "mode": "warm-context-reset",
        }.items():
            if raw.get(field) != expected:
                raise RuntimeError(f"Raw result metadata mismatch in {raw_path}: {field}")
        response = row.get("response", raw.get("response", ""))
        predicted = strict_classification(response)
        diagnostic = lenient_classification(response)
        row.update({
            "pair_id": record["pair_id"],
            "difficulty": record["difficulty"],
            "expected_label": record["expected_label"],
            "predicted_label": predicted,
            "classification_valid": predicted is not None,
            "classification_correct": predicted == record["expected_label"] if predicted is not None else False,
            **binary_fields(diagnostic, record["expected_label"]),
        })
        write_json(row_path, row)
        rows.append(row)
    if not rows:
        raise RuntimeError(
            f"Warm NPU runner failed for {record['pair_id']}/{record['difficulty']}: "
            f"{process.stdout[-500:]} {process.stderr[-500:]}"
        )
    run_error = None if process.returncode == 0 and len(rows) == args.repetitions else "Measured run session ended before all repetitions completed"
    result = build_image_report(record, rows, round(time.monotonic() - started, 3), args.repetitions, run_error)
    write_json(image_dir / "image-report.json", result)
    if result["status"] == "completed":
        write_json(completed, result)
    return result


def numeric_stats(rows: list[dict]) -> dict:
    result = {}
    for metric in METRICS:
        values = [float(row[metric]) for row in rows if row.get(metric) is not None]
        result[f"mean_{metric}"] = statistics.mean(values) if values else None
        result[f"sd_{metric}"] = statistics.stdev(values) if len(values) > 1 else None
    return result


def classification_stats(rows: list[dict], scheduled: int) -> dict:
    observed = len(rows)
    valid_rows = [row for row in rows if row.get("classification_valid")]
    correct = sum(bool(row.get("classification_correct")) for row in rows)
    predictions = [row.get("predicted_label") or "invalid" for row in rows]
    predictions.extend(["failed"] * max(0, scheduled - observed))
    counts = Counter(predictions)
    valid_counts = Counter(row["predicted_label"] for row in valid_rows)
    diagnostic_rows = [row for row in rows if row.get("diagnostic_valid")]
    diagnostic_correct = sum(bool(row.get("diagnostic_correct")) for row in rows)
    diagnostic_counts = Counter([row.get("diagnostic_label") or "invalid" for row in rows])
    diagnostic_counts.update({"failed": max(0, scheduled - observed)})
    diagnostic_valid_counts = Counter(row["diagnostic_label"] for row in diagnostic_rows)
    return {
        "scheduled": scheduled,
        "observed": observed,
        "attempted": scheduled,
        "failed_runs": scheduled - observed,
        "valid": len(valid_rows),
        "invalid": scheduled - len(valid_rows),
        "correct": correct,
        "accuracy_all_attempts": correct / scheduled if scheduled else None,
        "accuracy_valid_only": correct / len(valid_rows) if valid_rows else None,
        "prediction_counts": dict(sorted(counts.items())),
        "prediction_consistency": max(counts.values()) / scheduled if scheduled else None,
        "valid_prediction_consistency": max(valid_counts.values()) / len(valid_rows) if valid_rows else None,
        "diagnostic_valid": len(diagnostic_rows),
        "diagnostic_invalid": scheduled - len(diagnostic_rows),
        "diagnostic_correct": diagnostic_correct,
        "diagnostic_accuracy_all_attempts": diagnostic_correct / scheduled if scheduled else None,
        "diagnostic_accuracy_valid_only": diagnostic_correct / len(diagnostic_rows) if diagnostic_rows else None,
        "diagnostic_prediction_counts": dict(sorted(diagnostic_counts.items())),
        "diagnostic_prediction_consistency": max(diagnostic_counts.values()) / scheduled if scheduled else None,
        "diagnostic_valid_prediction_consistency": max(diagnostic_valid_counts.values()) / len(diagnostic_rows) if diagnostic_rows else None,
        "binary_correct": sum(bool(row.get("binary_correct")) for row in rows),
        "binary_accuracy_all_attempts": sum(bool(row.get("binary_correct")) for row in rows) / scheduled if scheduled else None,
    }


def build_image_report(record: dict, rows: list[dict], elapsed_s: float, scheduled: int, error: str | None = None) -> dict:
    return {
        "status": "completed" if error is None else "partial",
        "pair_id": record["pair_id"],
        "difficulty": record["difficulty"],
        "expected_label": record["expected_label"],
        "source": record["source"],
        "prepared": record["prepared"],
        "remote_filename": record["remote_filename"],
        "elapsed_s": elapsed_s,
        "error": error,
        **classification_stats(rows, scheduled),
        **numeric_stats(rows),
    }


def aggregate_reports(reports: list[dict]) -> dict:
    usable = [report for report in reports if report.get("status") in {"completed", "partial"}]
    result = {"images": len(reports), "completed_images": sum(report.get("status") == "completed" for report in reports), "failed_images": sum(report.get("status") == "error" for report in reports), "partial_images": sum(report.get("status") == "partial" for report in reports)}
    for key in ("original", "realistic", "obvious"):
        group = [report for report in usable if report["difficulty"] == key]
        attempted = sum(report["attempted"] for report in group)
        valid = sum(report["valid"] for report in group)
        correct = sum(report["correct"] for report in group)
        diagnostic_valid = sum(report["diagnostic_valid"] for report in group)
        diagnostic_correct = sum(report["diagnostic_correct"] for report in group)
        binary_correct = sum(report.get("binary_correct", 0) for report in group)
        result[key] = {
            "images": len(group),
            "attempted": attempted,
            "valid": valid,
            "invalid": attempted - valid,
            "correct": correct,
            "binary_correct": binary_correct,
            "binary_accuracy_all_attempts": binary_correct / attempted if attempted else None,
            "accuracy_all_attempts": correct / attempted if attempted else None,
            "accuracy_valid_only": correct / valid if valid else None,
            "diagnostic_valid": diagnostic_valid,
            "diagnostic_invalid": attempted - diagnostic_valid,
            "diagnostic_correct": diagnostic_correct,
            "diagnostic_accuracy_all_attempts": diagnostic_correct / attempted if attempted else None,
            "diagnostic_accuracy_valid_only": diagnostic_correct / diagnostic_valid if diagnostic_valid else None,
        }
    result["overall"] = {
        "attempted": sum(report["attempted"] for report in usable),
        "valid": sum(report["valid"] for report in usable),
        "invalid": sum(report["invalid"] for report in usable),
        "correct": sum(report["correct"] for report in usable),
        "diagnostic_valid": sum(report["diagnostic_valid"] for report in usable),
        "diagnostic_correct": sum(report["diagnostic_correct"] for report in usable),
        "binary_correct": sum(report.get("binary_correct", 0) for report in usable),
    }
    result["overall"]["binary_accuracy_all_attempts"] = (
        result["overall"]["binary_correct"] / result["overall"]["attempted"]
        if result["overall"]["attempted"] else None
    )
    result["overall"]["accuracy_all_attempts"] = (
        result["overall"]["correct"] / result["overall"]["attempted"] if result["overall"]["attempted"] else None
    )
    result["overall"]["accuracy_valid_only"] = (
        result["overall"]["correct"] / result["overall"]["valid"] if result["overall"]["valid"] else None
    )
    result["overall"]["diagnostic_invalid"] = result["overall"]["attempted"] - result["overall"]["diagnostic_valid"]
    result["overall"]["diagnostic_accuracy_all_attempts"] = (
        result["overall"]["diagnostic_correct"] / result["overall"]["attempted"] if result["overall"]["attempted"] else None
    )
    result["overall"]["diagnostic_accuracy_valid_only"] = (
        result["overall"]["diagnostic_correct"] / result["overall"]["diagnostic_valid"] if result["overall"]["diagnostic_valid"] else None
    )
    return result


def write_report(output: Path, settings: dict, reports: list[dict]) -> dict:
    report = {
        "schema_version": 1,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "settings": settings,
        "images": reports,
        "summary": aggregate_reports(reports),
    }
    write_json(output / "accuracy_report.json", report)
    lines = [
        "# Accuracy pilot",
        "",
        f"Model: `{settings['model']}`  ",
        f"Backend: `NPU`; repetitions: {settings['repetitions']} measured after one excluded warm-up  ",
        f"Prompt: `{settings['prompt']}`",
        "",
        "Strict JSON measures output-format compliance only, and is not correctness when the prompt invites freeform text. The lenient parser reads a verdict out of prose, tolerating casing, hedging, and negation; `real vs manipulated` collapses synthetic and tampered into one positive class.",
        "",
        "| Level | Images | Attempts | Strict JSON valid | Strict JSON all | Lenient parsed | Lenient 3-class | Real vs manipulated |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    summary = report["summary"]
    for level in ("original", "realistic", "obvious", "overall"):
        item = summary[level]
        lines.append(
            f"| {level} | {item.get('images', '-') } | {item['attempted']} | {item['valid']} | {format_rate(item['accuracy_all_attempts'])} | {item['diagnostic_valid']} | {format_rate(item.get('diagnostic_accuracy_all_attempts'))} | {format_rate(item.get('binary_accuracy_all_attempts'))} |"
        )
    lines += ["", "Agreement below is measured across parsed runs only; it is blank when nothing parsed, so it can no longer read as 100% simply because every run failed to parse.", "", "## Per-image results", "", "| Pair | Level | Lenient predictions | Parsed | Agreement | TTFT mean ms | Decode mean tok/s |", "|---|---|---|---:|---:|---:|---:|"]
    for item in reports:
        if item.get("status") != "completed":
            lines.append(f"| {item.get('pair_id', '?')} | {item.get('difficulty', '?')} | {item.get('status', 'error')} | — | — | — | — |")
            continue
        diagnostic = ", ".join(f"{label}={count}" for label, count in item["diagnostic_prediction_counts"].items() if count)
        lines.append(f"| {item['pair_id']} | {item['difficulty']} | {diagnostic} | {item['diagnostic_valid']} | {format_rate(item['diagnostic_valid_prediction_consistency'])} | {format_number(item['mean_ttft_ms'])} | {format_number(item['mean_decode_tok_s'])} |")
    lines += ["", "Raw per-run responses and SDK records are under each image checkpoint directory.", ""]
    (output / "accuracy_report.md").write_text("\n".join(lines), encoding="utf-8")
    return report


def format_rate(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.1f}%"


def format_number(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--settings", type=Path, default=REPO_ROOT / "accuracy_pilot_defaults.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--pairs")
    parser.add_argument("--repetitions", type=int)
    parser.add_argument("--max-edge", type=int)
    parser.add_argument("--model")
    parser.add_argument("--prompt")
    parser.add_argument("--model-already-downloaded", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--rest-seconds", type=float)
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args()
    args.dataset = args.dataset.resolve(strict=True)
    settings = load_settings(args.settings.resolve(strict=True))
    run = settings["run"]
    args.model = args.model or run["model"]
    args.prompt = args.prompt or run["prompt"]
    args.repetitions = int(run["repetitions"]) if args.repetitions is None else args.repetitions
    args.rest_seconds = run["rest_seconds"] if args.rest_seconds is None else args.rest_seconds
    args.model_already_downloaded = run["model_already_downloaded"] if args.model_already_downloaded is None else args.model_already_downloaded
    max_edge = int(settings["preprocessing"]["max_edge"]) if args.max_edge is None else args.max_edge
    if args.repetitions < 1 or args.rest_seconds < 0 or max_edge < 1:
        parser.error("repetitions, max edge, and rest seconds must be valid")
    manifest_path = (args.manifest or args.dataset / "accuracy_manifest.json").resolve()
    # Rebuild from source hashes each invocation; stale prepared files are rejected.
    manifest = build_manifest(args.dataset, args.dataset / f"prepared_{max_edge}", max_edge)
    write_json(manifest_path, manifest)
    available = list(manifest["pairs"])
    try:
        selected_pairs = parse_pairs(args.pairs or ",".join(run["pairs"]), available)
    except ValueError as error:
        parser.error(str(error))
    args.output = (args.output or args.dataset / f"accuracy_pilot_{datetime.now().strftime('%Y%m%d_%H%M%S')}").resolve()
    args.output.mkdir(parents=True, exist_ok=True)
    run_settings = {
        "dataset": str(args.dataset), "manifest": str(manifest_path), "pairs": selected_pairs,
        "model": args.model, "prompt": args.prompt, "backend": "NPU", "mode": "warm-context-reset",
        "model_already_downloaded": args.model_already_downloaded, "repetitions": args.repetitions,
        "rest_seconds": args.rest_seconds, "max_edge": max_edge,
    }
    settings_path = args.output / "run-settings.json"
    if settings_path.exists() and read_json(settings_path) != run_settings:
        parser.error("Existing output settings differ; choose a new --output directory")
    write_json(settings_path, run_settings)
    records = [record for record in manifest["records"] if record["pair_id"] in selected_pairs]
    records.sort(key=lambda record: (selected_pairs.index(record["pair_id"]), ("original", "realistic", "obvious").index(record["difficulty"])))
    reports = []
    for record in records:
        image_dir = args.output / f"{record['pair_id']}__{record['difficulty']}"
        try:
            result = run_image(record, image_dir, args)
            reports.append(result)
            print(f"DONE {record['pair_id']}/{record['difficulty']}", flush=True)
        except Exception as error:
            failure = {"status": "error", "pair_id": record["pair_id"], "difficulty": record["difficulty"], "error": str(error)}
            write_json(image_dir / "error.json", failure)
            reports.append(failure)
            print(f"FAILED {record['pair_id']}/{record['difficulty']}: {error}", file=sys.stderr, flush=True)
            if args.fail_fast:
                write_report(args.output, run_settings, reports)
                raise
        write_report(args.output, run_settings, reports)
    write_report(args.output, run_settings, reports)
    print(f"Report: {args.output / 'accuracy_report.md'}")


if __name__ == "__main__":
    main()
