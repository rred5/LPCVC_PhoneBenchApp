"""Score finished two-stage runs and write metrics, tables, and figures.

    py analyze_two_stage.py --manifest manifest.json --output out \\
        --run llama_cpp=runs/llama_cpp-npu --run qairt=runs/qairt

Each --run is a folder made by run_two_stage_series.py. Up to three runs are compared. Writes
into --output: metrics.json, report.md, scored_<label>.csv (the numbers behind every figure),
and the figures from plot_two_stage.py. Scoring needs only the standard library; the figures
need numpy and matplotlib (see requirements-analysis.txt).
"""

import argparse
import csv
import json
import math
from pathlib import Path
import statistics

from tamper_dataset import load_manifest
from two_stage import calibrate_convention, parse_stage2, score_iteration


MAX_RUNS = 3
CONVENTION_CHOICES = ("calibrate", "auto", "absolute", "norm1000", "unit")
STABILITY_WINDOW = 7
STABILITY_TOLERANCE = 0.10
STABILITY_TAIL = 0.25


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def describe(values: list) -> dict:
    """Count, mean, median, spread and range of the usable numbers in values."""
    numbers = [float(v) for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    if not numbers:
        return {"n": 0, "mean": None, "median": None, "std": None, "min": None, "max": None}
    return {"n": len(numbers), "mean": statistics.fmean(numbers), "median": statistics.median(numbers),
            "std": statistics.pstdev(numbers), "min": min(numbers), "max": max(numbers)}


def rate(flags: list) -> float | None:
    flags = [flag for flag in flags if flag is not None]
    return sum(bool(flag) for flag in flags) / len(flags) if flags else None


def rolling_median(values: list[float], window: int) -> list[float | None]:
    """Median of the last `window` values; None until a full window exists."""
    return [statistics.median(values[i - window + 1:i + 1]) if i + 1 >= window else None for i in range(len(values))]


def find_stabilization(values: list[float], window: int = STABILITY_WINDOW, tolerance: float = STABILITY_TOLERANCE,
                       tail_fraction: float = STABILITY_TAIL) -> dict | None:
    """Where a rising series levels off.

    The steady level is the median of the last quarter of the series. The series has stabilized
    at the first iteration after which its rolling median never leaves +/- `tolerance` of that
    level. Returns None when there are too few points to say.
    """
    values = [v for v in values if v is not None]
    if len(values) < 3 * window:
        return None
    steady = statistics.median(values[-max(window, math.ceil(tail_fraction * len(values))):])
    smooth = rolling_median(values, window)
    first_ok = len(values) - 1
    for index in range(len(values) - 1, window - 2, -1):
        if abs(smooth[index] - steady) <= tolerance * steady:
            first_ok = index
        else:
            break
    initial = statistics.median(values[:window])
    if abs(initial - steady) <= tolerance * steady:
        first_ok = 0  # never left the band: no drift to report
    drifted = abs(initial - steady) > tolerance * steady
    held = len(values) - first_ok
    # The steady level is the median of the last quarter, so a series that only entered the band
    # inside that quarter has not shown a plateau, it has just run out of images.
    plateau_reached = not drifted or held >= max(2 * window, math.ceil(tail_fraction * len(values)))
    return {"iteration": first_ok + 1, "steady": steady, "initial": initial,
            "change_pct": (steady - initial) / initial * 100 if initial else None,
            "drifted": drifted, "held_images": held, "plateau_reached": plateau_reached,
            "window": window, "tolerance": tolerance, "points": len(values)}


def load_run(directory: Path, records: dict[str, dict]) -> dict:
    iterations = sorted(read_jsonl(directory / "iterations.jsonl"), key=lambda row: row["iteration"])
    missing = sorted({row["record_id"] for row in iterations} - set(records))
    if missing:
        raise ValueError(f"{directory}: images not in the manifest: {missing[:3]}")
    return {"directory": directory, "settings": read_json(directory / "run-settings.json"),
            "summary": read_json(directory / "summary.json"), "iterations": iterations,
            "thermal": read_jsonl(directory / "thermal.jsonl")}


def choose_convention(run: dict, records: dict[str, dict], requested: str) -> tuple[str, dict | None]:
    """The coordinate scale to use for a run, calibrated on its own tampered images when asked."""
    if requested != "calibrate":
        return requested, None
    samples = []
    for row in run["iterations"]:
        record = records[row["record_id"]]
        quality = record.get("gt_quality") or {}
        if record["expected_label"] != "tampered" or not record.get("gt_box_norm") or quality.get("low_confidence"):
            continue
        stage2 = row.get("stage2") or {}
        boxes = [entry["box"] for entry in parse_stage2(stage2.get("response", ""))["boxes"]] if stage2.get("status") == "completed" else []
        if boxes:
            width, height = record["prepared_size"]
            samples.append((boxes, width, height, record["gt_box_norm"]))
    if not samples:
        return "absolute", None
    calibration = calibrate_convention(samples)
    return calibration["best"], calibration


def score_run(run: dict, records: dict[str, dict], requested: str) -> dict:
    convention, calibration = choose_convention(run, records, requested)
    rows = []
    for position, item in enumerate(run["iterations"], start=1):
        row = score_iteration(records[item["record_id"]], item, convention)
        row["iteration"] = position
        row["seen_s"] = item.get("seen_s")
        row["record_id"] = item["record_id"]
        rows.append(row)
    return {**run, "rows": rows, "convention": convention, "calibration": calibration}


def group_rate(rows: list[dict], key: str, flag: str) -> dict:
    groups: dict[str, list] = {}
    for row in rows:
        if row[key] is not None and row[flag] is not None:
            groups.setdefault(row[key], []).append(row[flag])
    return {name: {"rate": rate(flags), "n": len(flags)} for name, flags in sorted(groups.items())}


def summarize_run(scored: dict) -> dict:
    rows = scored["rows"]
    tampered = [row for row in rows if row["expected_label"] == "tampered"]
    real = [row for row in rows if row["expected_label"] == "real"]
    boxed = [row for row in tampered if row["box_scored"]]
    both = [row for row in rows if row["total_inference_s"] is not None]
    confusion: dict[str, dict[str, int]] = {}
    for row in rows:
        confusion.setdefault(row["expected_label"], {}).setdefault(row["predicted_label"] or "invalid", 0)
        confusion[row["expected_label"]][row["predicted_label"] or "invalid"] += 1
    seen = [row["seen_s"] for row in rows if row["seen_s"] is not None]
    metrics = {
        "label": scored["settings"].get("label"), "model": scored["settings"].get("model"),
        "runtime": scored["settings"].get("runtime"), "backend": scored["settings"].get("backend"),
        "device": scored["settings"].get("device"),
        "images": len(rows), "tampered": len(tampered), "real": len(real),
        "stage1_failed": sum(row["stage1_status"] != "completed" for row in rows),
        "stage2_missing": sum(row["stage2_status"] != "completed" for row in rows),
        "box_convention": scored["convention"], "calibration": scored["calibration"],
        "timing": {
            "stage1_s": describe([row["stage1_inference_s"] for row in rows]),
            "stage2_s": describe([row["stage2_inference_s"] for row in rows]),
            "per_image_s": describe([row["total_inference_s"] for row in both]),
            "board_total_s": sum(row["total_inference_s"] for row in both),
            "host_total_s": scored["summary"].get("host_total_s"),
            "host_span_s": (max(seen) - min(seen)) if len(seen) > 1 else None,
            "stage1_ttft_s": describe([row["stage1_ttft_s"] for row in rows]),
            "stage2_ttft_s": describe([row["stage2_ttft_s"] for row in rows]),
            "stage1_decode_tok_s": describe([row["stage1_decode_tok_s"] for row in rows]),
            "stage2_decode_tok_s": describe([row["stage2_decode_tok_s"] for row in rows]),
        },
        # Stage 1 does comparable work for every image (one image prefill, a short answer). Stage 2
        # is short for images called real and longer when it draws boxes, so the both-stage total
        # swings with the real/tampered mix and levels off later than the phone really does.
        "stabilization": {
            "stage1_s": find_stabilization([row["stage1_inference_s"] for row in rows]),
            "stage1_ms_per_token": find_stabilization([row["stage1_ms_per_token"] for row in rows]),
            "per_image_s": find_stabilization([row["total_inference_s"] for row in rows]),
        },
        "temperature_c": {"first": next((r["battery_c_after"] for r in rows if r["battery_c_after"] is not None), None),
                          "last": next((r["battery_c_after"] for r in reversed(rows) if r["battery_c_after"] is not None), None),
                          "max": max((r["battery_c_after"] for r in rows if r["battery_c_after"] is not None), default=None)},
        "verdict": {
            "three_class_accuracy": rate([row["label_correct"] for row in rows]),
            "binary_accuracy": rate([row["binary_correct"] for row in rows]),
            "sensitivity": rate([row["binary_correct"] for row in tampered]),
            "specificity": rate([row["binary_correct"] for row in real]),
            "tampered_called_tampered": rate([row["label_correct"] for row in tampered]),
            "sensitivity_by_edit": group_rate(tampered, "edit", "binary_correct"),
            "sensitivity_by_source": group_rate(tampered, "source_kind", "binary_correct"),
            "edit_named_correctly": rate([row["edit_correct"] for row in tampered]),
            "confusion": confusion,
        },
        "parsing": {
            "stage1_schema_ok": rate([row["schema_ok"] for row in rows]),
            "stage1_strict_json": rate([row["label_parse"] == "strict" for row in rows]),
            "stage1_no_verdict": rate([row["predicted_label"] is None for row in rows]),
            "stage2_json": rate([row["boxes_parse"] in ("strict", "embedded") for row in rows]),
            "stage2_declared_empty_on_real": rate([row["boxes_declared_empty"] for row in real]),
        },
        "boxes": {
            "scored": len(boxed),
            "excluded_low_confidence": sum(1 for row in tampered if not row["box_scored"]),
            "mean_best_iou": describe([row["best_iou"] for row in boxed])["mean"],
            "median_best_iou": describe([row["best_iou"] for row in boxed])["median"],
            "hit_at_50": rate([row["hit50"] for row in boxed]),
            "center_hit": rate([row["center_hit"] for row in boxed]),
            "no_box_on_tampered": rate([row["n_boxes"] == 0 for row in boxed]),
            "by_edit": {name: {"mean_best_iou": describe([r["best_iou"] for r in boxed if r["edit"] == name])["mean"],
                               "n": sum(1 for r in boxed if r["edit"] == name)} for name in sorted({r["edit"] for r in boxed})},
            "by_source": {name: {"mean_best_iou": describe([r["best_iou"] for r in boxed if r["source_kind"] == name])["mean"],
                                 "n": sum(1 for r in boxed if r["source_kind"] == name)} for name in sorted({r["source_kind"] for r in boxed})},
            "false_positive_on_real": rate([row["false_positive_boxes"] for row in real]),
        },
        "tokens": {
            "stage1_generated": describe([row["stage1_generated_tokens"] for row in rows]),
            "stage2_generated": describe([row["stage2_generated_tokens"] for row in rows]),
            "stage1_prompt": describe([row["stage1_prompt_tokens"] for row in rows]),
            "stage2_prompt": describe([row["stage2_prompt_tokens"] for row in rows]),
            "stage2_generated_tampered": describe([row["stage2_generated_tokens"] for row in tampered])["mean"],
            "stage2_generated_real": describe([row["stage2_generated_tokens"] for row in real])["mean"],
            "stop_reasons": {stage: _count([row[f"{stage}_stop"] for row in rows]) for stage in ("stage1", "stage2")},
        },
    }
    return metrics


def _count(values: list) -> dict:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.0f}%"


def _num(value: float | None, digits: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{digits}f}"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def write_scored_csv(scored: dict, path: Path) -> None:
    rows = scored["rows"]
    columns = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8-sig") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def render_thermal_lines(all_metrics: list[dict]) -> list[str]:
    """One bullet per run and series saying whether the time drifted and where it levels off."""
    lines = []
    for m in all_metrics:
        for name, series in (("stage 1 time per image", "stage1_s"), ("stage 1 ms per token", "stage1_ms_per_token"),
                             ("time per image, both stages", "per_image_s")):
            s = m["stabilization"][series]
            if s is None:
                lines.append(f"- **{m['label']}**, {name}: too few images to judge (needs {3 * STABILITY_WINDOW}).")
            elif not s["drifted"]:
                lines.append(f"- **{m['label']}**, {name}: no drift beyond {s['tolerance'] * 100:.0f}% (steady near {s['steady']:.1f}).")
            elif not s["plateau_reached"]:
                lines.append(f"- **{m['label']}**, {name}: moved {s['change_pct']:+.0f}% (from {s['initial']:.1f} to {s['steady']:.1f}) and was "
                             f"still moving toward the end: it came within {s['tolerance'] * 100:.0f}% of its final level only for the last "
                             f"{s['held_images']} of {s['points']} images, so no plateau is shown. A longer run is needed.")
            else:
                lines.append(f"- **{m['label']}**, {name}: moved {s['change_pct']:+.0f}% (from {s['initial']:.1f} to {s['steady']:.1f}) and held "
                             f"within {s['tolerance'] * 100:.0f}% from image {s['iteration']} on.")
        t = m["temperature_c"]
        lines.append(f"- **{m['label']}**, battery temperature: {_num(t['first'])} C to {_num(t['last'])} C (max {_num(t['max'])} C).")
    return lines


def write_report(all_metrics: list[dict], path: Path, figures: list[str]) -> None:
    lines = ["# Two-stage tamper run report", ""]
    header = ["", *[m["label"] for m in all_metrics]]
    table = lambda rows: ["| " + " | ".join(header) + " |", "|" + "---|" * len(header),
                          *["| " + " | ".join([name, *values]) + " |" for name, values in rows]]
    lines += ["## Setup", ""] + table([
        ("Model", [str(m["model"]) for m in all_metrics]),
        ("Runtime / backend", [f"{m['runtime']} / {m['backend']}" for m in all_metrics]),
        ("Phone", [str((m["device"] or {}).get("model")) + " " + str((m["device"] or {}).get("soc")) for m in all_metrics]),
        ("Images (tampered / real)", [f"{m['images']} ({m['tampered']} / {m['real']})" for m in all_metrics]),
        ("Failed stage 1 / missing stage 2", [f"{m['stage1_failed']} / {m['stage2_missing']}" for m in all_metrics]),
    ])
    lines += ["", "## Timing (seconds of inference reported by the phone)", ""] + table([
        ("Stage 1 mean", [_num(m["timing"]["stage1_s"]["mean"]) for m in all_metrics]),
        ("Stage 2 mean", [_num(m["timing"]["stage2_s"]["mean"]) for m in all_metrics]),
        ("Per image mean", [_num(m["timing"]["per_image_s"]["mean"]) for m in all_metrics]),
        ("Total, board", [_duration(m["timing"]["board_total_s"]) for m in all_metrics]),
        ("Total, host (whole run incl. load and rests)", [_duration(m["timing"]["host_total_s"]) for m in all_metrics]),
        ("Stage 1 time to first token mean", [_num(m["timing"]["stage1_ttft_s"]["mean"]) for m in all_metrics]),
        ("Stage 2 time to first token mean", [_num(m["timing"]["stage2_ttft_s"]["mean"]) for m in all_metrics]),
        ("Stage 1 decode tok/s", [_num(m["timing"]["stage1_decode_tok_s"]["mean"]) for m in all_metrics]),
        ("Stage 2 decode tok/s", [_num(m["timing"]["stage2_decode_tok_s"]["mean"]) for m in all_metrics]),
    ])
    lines += ["", "## Thermal drift", ""]
    lines += ["Judged on stage 1 time and decode time per token, which do comparable work for every image. "
              "The both-stage total also depends on how long stage 2 runs, so it settles later and less cleanly.", ""]
    lines += render_thermal_lines(all_metrics)
    lines += ["", "## Verdict", ""] + table([
        ("3-class accuracy", [_pct(m["verdict"]["three_class_accuracy"]) for m in all_metrics]),
        ("Real vs manipulated accuracy", [_pct(m["verdict"]["binary_accuracy"]) for m in all_metrics]),
        ("Sensitivity (manipulated found)", [_pct(m["verdict"]["sensitivity"]) for m in all_metrics]),
        ("Specificity (real called real)", [_pct(m["verdict"]["specificity"]) for m in all_metrics]),
        ("Tampered called 'tampered'", [_pct(m["verdict"]["tampered_called_tampered"]) for m in all_metrics]),
        ("Edit type named correctly", [_pct(m["verdict"]["edit_named_correctly"]) for m in all_metrics]),
        ("Stage 1 followed the JSON schema", [_pct(m["parsing"]["stage1_schema_ok"]) for m in all_metrics]),
        ("Stage 1 gave no usable verdict", [_pct(m["parsing"]["stage1_no_verdict"]) for m in all_metrics]),
    ])
    lines += ["", "## Bounding boxes (tampered images)", ""] + table([
        ("Coordinate scale used", [str(m["box_convention"]) for m in all_metrics]),
        ("Images scored (low-confidence masks excluded)", [f"{m['boxes']['scored']} ({m['boxes']['excluded_low_confidence']} excluded)" for m in all_metrics]),
        ("Mean best IoU", [_num(m["boxes"]["mean_best_iou"], 2) for m in all_metrics]),
        ("Median best IoU", [_num(m["boxes"]["median_best_iou"], 2) for m in all_metrics]),
        ("Hit at IoU 0.5", [_pct(m["boxes"]["hit_at_50"]) for m in all_metrics]),
        ("Box centre inside the truth", [_pct(m["boxes"]["center_hit"]) for m in all_metrics]),
        ("No box at all", [_pct(m["boxes"]["no_box_on_tampered"]) for m in all_metrics]),
        ("Boxes drawn on real images", [_pct(m["boxes"]["false_positive_on_real"]) for m in all_metrics]),
    ])
    lines += ["", "## Tokens generated", ""] + table([
        ("Stage 1 mean (max)", [f"{_num(m['tokens']['stage1_generated']['mean'])} ({_num(m['tokens']['stage1_generated']['max'], 0)})" for m in all_metrics]),
        ("Stage 2 mean (max)", [f"{_num(m['tokens']['stage2_generated']['mean'])} ({_num(m['tokens']['stage2_generated']['max'], 0)})" for m in all_metrics]),
        ("Stage 2 mean, tampered / real", [f"{_num(m['tokens']['stage2_generated_tampered'])} / {_num(m['tokens']['stage2_generated_real'])}" for m in all_metrics]),
        ("Stage 1 prompt tokens mean", [_num(m["tokens"]["stage1_prompt"]["mean"], 0) for m in all_metrics]),
        ("Stage 2 prompt tokens mean", [_num(m["tokens"]["stage2_prompt"]["mean"], 0) for m in all_metrics]),
    ])
    lines += ["", "Stop reasons: " + "; ".join(f"{m['label']} {json.dumps(m['tokens']['stop_reasons'])}" for m in all_metrics), ""]
    lines += ["## Figures", ""] + [f"- `{name}`" for name in figures]
    lines += ["", "Every figure's numbers are in `scored_<label>.csv`; `metrics.json` has all summary values.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run", action="append", required=True, metavar="LABEL=DIR",
                        help="A run folder from run_two_stage_series.py; repeat for up to three runs")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--convention", choices=CONVENTION_CHOICES, default="calibrate",
                        help="Coordinate scale of the model's boxes; 'calibrate' picks the best fit per run")
    parser.add_argument("--theme", choices=("light", "dark"), default="light")
    parser.add_argument("--no-figures", action="store_true")
    args = parser.parse_args()
    if len(args.run) > MAX_RUNS:
        parser.error(f"At most {MAX_RUNS} runs can be compared (the palette is validated for {MAX_RUNS})")
    pairs = []
    for spec in args.run:
        label, separator, folder = spec.partition("=")
        if not separator or not label or not folder:
            parser.error(f"--run must look like LABEL=DIR, got '{spec}'")
        pairs.append((label, Path(folder)))
    if len({label for label, _ in pairs}) != len(pairs):
        parser.error("Run labels must be unique")

    manifest = load_manifest(args.manifest)
    records = {record["remote_filename"]: record for record in manifest["records"]}
    args.output.mkdir(parents=True, exist_ok=True)
    scored_runs, all_metrics = [], []
    for label, folder in pairs:
        run = load_run(folder, records)
        if not run["iterations"]:
            parser.error(f"{folder} has no recorded images yet")
        run["settings"] = {**run["settings"], "label": label}
        scored = score_run(run, records, args.convention)
        scored_runs.append(scored)
        all_metrics.append(summarize_run(scored))
        write_scored_csv(scored, args.output / f"scored_{label}.csv")

    figures: list[str] = []
    if not args.no_figures:
        from plot_two_stage import make_figures
        figures = make_figures(scored_runs, all_metrics, args.output, args.theme)
    (args.output / "metrics.json").write_text(json.dumps({"runs": all_metrics}, indent=2, default=str), encoding="utf-8")
    write_report(all_metrics, args.output / "report.md", figures)
    print(f"Wrote {args.output / 'report.md'} and {len(figures)} figures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
