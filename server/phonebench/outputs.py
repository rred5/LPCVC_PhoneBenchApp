"""Build a job's output files in the harness's formats.

Files:

- `job.json`      — the job, its settings, images and prompts
- `runs.csv`      — one row per run, harness column names; for a multi-turn
                    prompt, the response and metrics are the first turn's
- `turns.csv`     — one row per turn, when the job has a multi-turn prompt
- `summary.json`  — mean and SD per model, compute unit and prompt
- `accuracy.json` — scores for labeled images, when the job has any
- `runs/<run_id>.json` — each run's raw report, when `save_raw_result` is on
- `outputs.zip`   — all of the above

They are built from the database whenever someone asks, so they are always
current. Local mode also writes them to `<data>/jobs/<job_id>/outputs/` when
a job finishes.
"""

import csv
from datetime import datetime
import io
import json
from pathlib import Path
import statistics
import zipfile

from .config import Config
from .scoring import score
from .settings import METRICS, turns
from .store import Store

OUTPUT_FILES = ("job.json", "runs.csv", "turns.csv", "summary.json", "accuracy.json", "outputs.zip")
STATUS_WORDS = {"completed": "passed", "error": "failed", "cancelled": "cancelled", "pending": "not_run"}
DEVICE_FIELDS = (
    "load_ms", "warm", "battery_c", "thermal_status", "free_memory_mb",
    "framework", "framework_version", "runtime", "compute_unit",
)
SCORE_FIELDS = (
    "expected_label", "predicted_label", "classification_valid", "classification_correct",
    "diagnostic_label", "diagnostic_valid", "diagnostic_correct",
    "binary_label", "binary_expected", "binary_correct",
)


def outputs_dir(cfg: Config, job_id: str) -> Path:
    return cfg.jobs_dir / job_id / "outputs"


def _elapsed(result: dict) -> float | None:
    try:
        started = datetime.fromisoformat(result["started_at"])
        finished = datetime.fromisoformat(result["finished_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return round((finished - started).total_seconds(), 2)


def _metric_values(result: dict, metrics: list[str]) -> dict:
    raw = result.get("metrics") or {}
    values = {}
    for metric in metrics:
        if metric == "reported_total_tokens":
            prompt, generated = raw.get("prompt_tokens"), raw.get("generated_tokens")
            values[metric] = prompt + generated if prompt is not None and generated is not None else None
        else:
            values[metric] = raw.get(metric)
    return values


def collect(store: Store, job_id: str) -> tuple[dict, list[dict]]:
    """Read a job and build one row per run, in the order the runs happened."""
    with store.read() as db:
        job = dict(db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone())
        images = {row["image_id"]: dict(row) for row in db.execute("SELECT * FROM job_images WHERE job_id = ? ORDER BY position", (job_id,))}
        prompts = {row["prompt_id"]: dict(row) for row in db.execute("SELECT * FROM prompts WHERE job_id = ? ORDER BY position", (job_id,))}
        runs = [
            dict(row)
            for row in db.execute(
                "SELECT runs.*, units.position AS unit_position FROM runs JOIN units ON units.unit_id = runs.unit_id"
                " WHERE runs.job_id = ?",
                (job_id,),
            )
        ]
    settings = json.loads(job["settings_json"])
    output = settings["output"]
    runs.sort(key=lambda run: (run["reported_at"] is None, run["reported_at"] or 0, run["unit_position"], run["position"]))
    rows = []
    for sequence, run in enumerate(runs, start=1):
        result = json.loads(run["result_json"]) if run["result_json"] else {}
        image = images[run["image_id"]]
        row = {
            "sequence": sequence,
            "run_id": run["run_id"],
            "model": run["model"],
            "backend": run["backend"],
            "mode": settings["run"]["mode"],
            "image": image["name"],
            "label": image["label"],
            "prompt": prompts[run["prompt_id"]]["position"] + 1,
            "repetition": run["repetition"],
            "turns": len(turns(settings["run"]["prompts"][prompts[run["prompt_id"]]["position"]])),
            "status": STATUS_WORDS[run["status"]],
            **_metric_values(result, output["metrics"]),
            **{field: result.get(field) for field in DEVICE_FIELDS},
            "started_utc": result.get("started_at"),
            "finished_utc": result.get("finished_at"),
            "elapsed_s": _elapsed(result),
            "error": run["error"] or result.get("error"),
            "_raw": result,
        }
        if output["save_response"]:
            row["response"] = result.get("response")
        if image["label"] and run["status"] == "completed":
            row.update(score(result.get("response") or "", image["label"]))
        rows.append(row)
    job["settings"] = settings
    job["images"] = [
        {key: image[key] for key in ("name", "label", "sha256", "source_sha256", "width", "height", "bytes")}
        for image in images.values()
    ]
    job["prompts"] = list(settings["run"]["prompts"])
    return job, rows


def _mean_sd(values: list[float]) -> tuple[float | None, float | None]:
    return (
        statistics.mean(values) if values else None,
        statistics.stdev(values) if len(values) > 1 else None,
    )


def summarize(rows: list[dict], metrics: list[str]) -> list[dict]:
    groups: dict[tuple, list[dict]] = {}
    for row in rows:
        groups.setdefault((row["model"], row["backend"], row["prompt"]), []).append(row)
    summaries = []
    for (model, backend, prompt), group in groups.items():
        attempted = [row for row in group if row["status"] in ("passed", "failed")]
        passed = [row for row in attempted if row["status"] == "passed"]
        summary = {
            "model": model, "backend": backend, "prompt": prompt,
            "attempted": len(attempted), "passed": len(passed), "failed": len(attempted) - len(passed),
        }
        for metric in metrics:
            mean, sd = _mean_sd([row[metric] for row in passed if row.get(metric) is not None])
            summary[f"mean_{metric}"], summary[f"sd_{metric}"] = mean, sd
        summaries.append(summary)
    return summaries


def _rate(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _accuracy_counts(rows: list[dict]) -> dict:
    attempted = len(rows)
    strict_valid = sum(bool(row.get("classification_valid")) for row in rows)
    strict_correct = sum(bool(row.get("classification_correct")) for row in rows)
    lenient_parsed = sum(bool(row.get("diagnostic_valid")) for row in rows)
    lenient_correct = sum(bool(row.get("diagnostic_correct")) for row in rows)
    binary_correct = sum(bool(row.get("binary_correct")) for row in rows)
    return {
        "attempted": attempted,
        "strict_valid": strict_valid,
        "strict_correct": strict_correct,
        "strict_accuracy": _rate(strict_correct, attempted),
        "lenient_parsed": lenient_parsed,
        "lenient_correct": lenient_correct,
        "lenient_accuracy": _rate(lenient_correct, attempted),
        "binary_correct": binary_correct,
        "binary_accuracy": _rate(binary_correct, attempted),
    }


def accuracy(rows: list[dict]) -> dict | None:
    """Scores per model, compute unit and prompt, overall and by label; None if nothing is labeled."""
    labeled = [row for row in rows if row["label"] and row["status"] in ("passed", "failed")]
    if not labeled:
        return None
    groups: dict[tuple, list[dict]] = {}
    for row in labeled:
        groups.setdefault((row["model"], row["backend"], row["prompt"]), []).append(row)
    result = []
    for (model, backend, prompt), group in groups.items():
        by_label = {}
        for row in group:
            by_label.setdefault(row["label"], []).append(row)
        result.append(
            {
                "model": model, "backend": backend, "prompt": prompt,
                **_accuracy_counts(group),
                "by_label": {label: _accuracy_counts(items) for label, items in sorted(by_label.items())},
            }
        )
    return {
        "note": "A failed run counts as attempted and incorrect. Strict needs a bare "
        '{"classification": ...} object; lenient reads a verdict from prose; binary is real vs manipulated.',
        "groups": result,
    }


TURN_COLUMNS = ("sequence", "run_id", "model", "backend", "image", "label", "prompt", "repetition", "turn", "status")


def turn_rows(rows: list[dict], metrics: list[str], save_response: bool) -> list[dict]:
    """One row per turn of each multi-turn run; a turn the phone never reached has no row."""
    result = []
    for row in rows:
        if row["turns"] < 2:
            continue
        for number, turn in enumerate(row["_raw"].get("turns") or [], start=1):
            result.append(
                {
                    **{key: row[key] for key in TURN_COLUMNS if key in row},
                    "turn": number,
                    "status": "failed" if turn.get("error") else "passed",
                    **_metric_values(turn, metrics),
                    **({"response": turn.get("response")} if save_response else {}),
                    "error": turn.get("error"),
                }
            )
    return result


def _csv_bytes(rows: list[dict], columns: list[str]) -> bytes:
    text = io.StringIO(newline="")
    writer = csv.DictWriter(text, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return text.getvalue().encode("utf-8-sig")


def _json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2) + "\n").encode("utf-8")


def build_files(store: Store, job_id: str) -> dict[str, bytes]:
    """Every output file of a job, by name; safe to call while the job is running."""
    job, rows = collect(store, job_id)
    output = job["settings"]["output"]
    files = {"job.json": _json_bytes({key: value for key, value in job.items() if key != "settings_json"})}
    if output["save_csv"]:
        scored = any("binary_correct" in row for row in rows)
        columns = [
            "sequence", "run_id", "model", "backend", "mode", "image", "label", "prompt", "repetition", "turns", "status",
            *output["metrics"], *DEVICE_FIELDS, "started_utc", "finished_utc", "elapsed_s",
            *(["response"] if output["save_response"] else []), "error",
            *(SCORE_FIELDS if scored else ()),
        ]
        files["runs.csv"] = _csv_bytes(rows, columns)
        if any(row["turns"] > 1 for row in rows):
            turn_columns = [*TURN_COLUMNS, *output["metrics"], *(["response"] if output["save_response"] else []), "error"]
            files["turns.csv"] = _csv_bytes(turn_rows(rows, output["metrics"], output["save_response"]), turn_columns)
    if output["save_summary"]:
        files["summary.json"] = _json_bytes(summarize(rows, output["metrics"]))
    scores = accuracy(rows)
    if scores is not None:
        files["accuracy.json"] = _json_bytes(scores)
    if output["save_raw_result"]:
        for row in rows:
            if row["_raw"]:
                files[f"runs/{row['run_id']}.json"] = _json_bytes(row["_raw"])
    return files


def zip_files(files: dict[str, bytes], folder: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            archive.writestr(f"{folder}/{name}", data)
    return buffer.getvalue()


def write_outputs(store: Store, cfg: Config, job_id: str) -> Path:
    """(Re)write a job's output files under the local data folder."""
    folder = outputs_dir(cfg, job_id)
    for name, data in build_files(store, job_id).items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)
    return folder
