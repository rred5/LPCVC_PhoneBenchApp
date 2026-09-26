"""Render presentation figures and a metrics summary from a rescored pilot.

Reads the pilot's ``accuracy_report.json`` plus the per-run rows, and the cold
backend benchmark ``summary.json``, then writes PNG figures sized for slides.

    py make_accuracy_figures.py --pilot <pilot_dir> --benchmark <bench_dir> \
        --output <figures_dir>
"""

import argparse
from collections import Counter
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SOFT = "#52514e"
GRID = "#d8d7d2"
SERIES = {"real": "#2a78d6", "synthetic": "#eb6834", "tampered": "#1baf7a"}
BACKEND_COLOR = {"CPU": "#2a78d6", "GPU": "#eb6834", "NPU": "#1baf7a"}
LEVELS = ("original", "realistic", "obvious")
LEVEL_TRUTH = {"original": "real", "realistic": "tampered", "obvious": "tampered"}
BAR = dict(linewidth=2, edgecolor=SURFACE, zorder=3)


def style(ax, title, ylabel=None):
    ax.set_facecolor(SURFACE)
    ax.set_title(title, color=INK, fontsize=13, fontweight="bold", loc="left", pad=12)
    if ylabel:
        ax.set_ylabel(ylabel, color=INK_SOFT, fontsize=10)
    ax.tick_params(colors=INK_SOFT, labelsize=10, length=0)
    ax.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right", "left"):
        ax.spines[side].set_visible(False)
    ax.spines["bottom"].set_color(GRID)


def save(fig, path):
    fig.patch.set_facecolor(SURFACE)
    fig.savefig(path, dpi=200, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    print(f"wrote {path}")


def load_rows(pilot: Path) -> list[dict]:
    rows = []
    for image_dir in sorted(path for path in pilot.iterdir() if path.is_dir()):
        runs = image_dir / "runs"
        if not runs.is_dir():
            continue
        for row_path in sorted(runs.glob("NPU-run-*.json")):
            if row_path.name.endswith("-result.json"):
                continue
            row = json.loads(row_path.read_text(encoding="utf-8"))
            if row.get("difficulty"):
                rows.append(row)
    order = {level: index for index, level in enumerate(LEVELS)}
    rows.sort(key=lambda row: (row["pair_id"], order[row["difficulty"]], row.get("run", 0)))
    return rows


def figure_predictions(rows, output):
    """What the model answered, grouped by ground truth."""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    labels = ["real", "synthetic", "tampered"]
    bottoms = [0.0] * len(LEVELS)
    for label in labels:
        heights = [
            sum(1 for row in rows if row["difficulty"] == level and row.get("diagnostic_label") == label)
            for level in LEVELS
        ]
        ax.bar(
            LEVELS, heights, bottom=bottoms, width=0.55,
            color=SERIES[label], label=f'answered "{label}"', **BAR,
        )
        for index, (height, base) in enumerate(zip(heights, bottoms)):
            if height:
                ax.text(index, base + height / 2, str(height), ha="center", va="center",
                        color="#ffffff", fontsize=11, fontweight="bold", zorder=4)
        bottoms = [base + height for base, height in zip(bottoms, heights)]
    for index, level in enumerate(LEVELS):
        ax.text(index, -0.9, f"truth: {LEVEL_TRUTH[level]}", ha="center",
                color=INK_SOFT, fontsize=9, style="italic")
    style(ax, "The model answers “real” regardless of ground truth", "Runs (3 images × 3 repetitions)")
    ax.set_ylim(0, 9.6)
    ax.legend(frameon=False, labelcolor=INK_SOFT, fontsize=10, ncol=3, loc="upper center",
              bbox_to_anchor=(0.5, -0.12))
    save(fig, output / "01_prediction_distribution.png")


def figure_sensitivity(rows, output):
    """Specificity is perfect only because the model never accuses."""
    real_rows = [row for row in rows if row["binary_expected"] == "real"]
    manip_rows = [row for row in rows if row["binary_expected"] == "manipulated"]
    specificity = sum(row["binary_correct"] for row in real_rows) / len(real_rows)
    sensitivity = sum(row["binary_correct"] for row in manip_rows) / len(manip_rows)

    fig, ax = plt.subplots(figsize=(8, 4.5))
    names = [f"Specificity\n(real images, n={len(real_rows)})",
             f"Sensitivity\n(manipulated, n={len(manip_rows)})"]
    values = [specificity, sensitivity]
    bars = ax.bar(names, values, width=0.45, color=["#2a78d6", "#eb6834"], **BAR)
    for bar, value in zip(bars, values):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.025, f"{value:.0%}",
                ha="center", color=INK, fontsize=15, fontweight="bold")
    ax.axhline(0.5, color=INK_SOFT, linewidth=1.2, linestyle=(0, (4, 3)), zorder=2)
    ax.text(0.5, 0.52, "coin flip", color=INK_SOFT, fontsize=9, ha="center")
    style(ax, "It never false-alarms, because it almost never accuses", "Rate")
    ax.set_ylim(0, 1.12)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    save(fig, output / "02_sensitivity_specificity.png")
    return specificity, sensitivity


def figure_accuracy_by_level(report, output):
    """Three-class and binary accuracy against an always-real baseline."""
    summary = report["summary"]
    groups = list(LEVELS) + ["overall"]
    three = [summary[key]["diagnostic_accuracy_all_attempts"] for key in groups]
    binary = [summary[key]["binary_accuracy_all_attempts"] for key in groups]
    baseline = [1.0, 0.0, 0.0, 9 / 27]

    fig, ax = plt.subplots(figsize=(9, 4.5))
    positions = range(len(groups))
    width = 0.34
    left = ax.bar([p - width / 2 for p in positions], three, width - 0.02,
                  color="#2a78d6", label="3-class (real / synthetic / tampered)", **BAR)
    right = ax.bar([p + width / 2 for p in positions], binary, width - 0.02,
                   color="#eb6834", label="binary (real vs manipulated)", **BAR)
    for bars, values in ((left, three), (right, binary)):
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, value + 0.02, f"{value:.0%}",
                    ha="center", color=INK, fontsize=10, fontweight="bold")
    for index, value in enumerate(baseline):
        ax.plot([index - 0.32, index + 0.32], [value, value], color=INK_SOFT,
                linewidth=1.6, linestyle=(0, (3, 2)), zorder=5,
                label="always-answer-“real” baseline" if index == 0 else None)
    style(ax, "Accuracy only beats the trivial baseline overall — and only in binary", "Accuracy")
    ax.set_xticks(list(positions), groups)
    ax.set_ylim(0, 1.18)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.legend(frameon=False, labelcolor=INK_SOFT, fontsize=9, ncol=3,
              loc="upper center", bbox_to_anchor=(0.5, -0.09))
    save(fig, output / "03_accuracy_by_difficulty.png")


def figure_backends(benchmark, output):
    """One panel per unit - never two y-scales on one axis."""
    summary = json.loads((benchmark / "summary.json").read_text(encoding="utf-8"))
    backends = [row["backend"] for row in summary]
    panels = [
        ("Time to first token", [row["mean_ttft_ms"] / 1000 for row in summary], "seconds", "{:.1f}s"),
        ("Prefill speed", [row["mean_prefill_tok_s"] for row in summary], "tokens/s", "{:.1f}"),
        ("Decode speed", [row["mean_decode_tok_s"] for row in summary], "tokens/s", "{:.1f}"),
    ]
    fig, axes = plt.subplots(1, 3, figsize=(12, 4.2))
    for ax, (title, values, unit, fmt) in zip(axes, panels):
        bars = ax.bar(backends, values, width=0.5,
                      color=[BACKEND_COLOR[b] for b in backends], **BAR)
        for bar, value in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, value + max(values) * 0.03,
                    fmt.format(value), ha="center", color=INK, fontsize=11, fontweight="bold")
        style(ax, title, unit)
        ax.set_ylim(0, max(values) * 1.22)
    fig.suptitle("Backend performance — cold, 768 px, 63 prompt tokens, 10 runs each",
                 color=INK, fontsize=13, fontweight="bold", x=0.09, ha="left", y=1.02)
    failures = [f"{row['backend']} {row['passed']}/{row['attempted']}" for row in summary
                if row["passed"] < row["attempted"]]
    if failures:
        fig.text(0.09, -0.04, "Completed runs: " + ", ".join(failures) + " — all others 10/10",
                 color=INK_SOFT, fontsize=9, ha="left")
    save(fig, output / "04_backend_performance.png")
    return summary


def figure_ttft_drift(rows, output):
    """TTFT climbs across a sustained NPU session, then plateaus."""
    values = [row["ttft_ms"] / 1000 for row in rows if row.get("ttft_ms") is not None]
    fig, ax = plt.subplots(figsize=(9, 4.2))
    ax.plot(range(1, len(values) + 1), values, color="#2a78d6", linewidth=2,
            marker="o", markersize=5, zorder=3)
    ax.axhline(values[0], color=INK_SOFT, linewidth=1.2, linestyle=(0, (4, 3)), zorder=2)
    ax.text(len(values), values[0] - 1.6, f"first run {values[0]:.1f}s",
            color=INK_SOFT, fontsize=9, ha="right")
    style(ax, "TTFT nearly doubles after the opening runs, then plateaus", "Time to first token (s)")
    ax.set_xlabel("Measured run, in execution order — run order confounds per-image timing",
                  color=INK_SOFT, fontsize=10)
    ax.set_ylim(0, max(values) * 1.15)
    save(fig, output / "05_ttft_drift.png")
    return values


def figure_parser_fix(report, output):
    """What the scoring bug hid."""
    summary = report["summary"]
    fig, ax = plt.subplots(figsize=(8.5, 4.5))
    names = ["Responses parsed", "3-class accuracy", "Real vs manipulated"]
    before = [0.0, 0.0, 0.0]
    after = [1.0, summary["overall"]["diagnostic_accuracy_all_attempts"],
             summary["overall"]["binary_accuracy_all_attempts"]]
    positions = range(len(names))
    width = 0.34
    ax.bar([p - width / 2 for p in positions], before, width - 0.02,
           color=GRID, label="original strict parser", **BAR)
    bars = ax.bar([p + width / 2 for p in positions], after, width - 0.02,
                  color="#2a78d6", label="corrected lenient parser", **BAR)
    for index, value in enumerate(before):
        ax.text(index - width / 2, 0.02, f"{value:.0%}", ha="center", color=INK_SOFT,
                fontsize=10, fontweight="bold")
    for bar, value in zip(bars, after):
        ax.text(bar.get_x() + bar.get_width() / 2, value + 0.02, f"{value:.0%}",
                ha="center", color=INK, fontsize=11, fontweight="bold")
    style(ax, "The reported 0% was a scoring artifact, not the model's score", "Rate over 27 runs")
    ax.set_xticks(list(positions), names)
    ax.set_ylim(0, 1.18)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.legend(frameon=False, labelcolor=INK_SOFT, fontsize=10, ncol=2,
              loc="upper center", bbox_to_anchor=(0.5, -0.09))
    save(fig, output / "06_parser_correction.png")


def write_metrics(report, rows, backend_summary, ttft, specificity, sensitivity, output):
    summary = report["summary"]
    counts = Counter(row.get("diagnostic_label") for row in rows)
    runaway = [row for row in rows if (row.get("generated_tokens") or 0) >= 2048]
    metrics = {
        "pilot": {
            "model": report["settings"]["model"],
            "backend": "NPU",
            "images": 9,
            "pairs": report["settings"]["pairs"],
            "runs": len(rows),
            "parsed": sum(1 for row in rows if row.get("diagnostic_valid")),
            "accuracy_3class": summary["overall"]["diagnostic_accuracy_all_attempts"],
            "accuracy_binary": summary["overall"]["binary_accuracy_all_attempts"],
            "baseline_always_real": 9 / 27,
            "specificity": specificity,
            "sensitivity": sensitivity,
            "balanced_accuracy": (specificity + sensitivity) / 2,
            "prediction_counts": dict(counts),
            "share_answered_real": counts.get("real", 0) / len(rows),
            "by_level": {
                level: {
                    "accuracy_3class": summary[level]["diagnostic_accuracy_all_attempts"],
                    "accuracy_binary": summary[level]["binary_accuracy_all_attempts"],
                }
                for level in LEVELS
            },
        },
        "performance": {
            "cold": {
                row["backend"]: {
                    "ttft_s": row["mean_ttft_ms"] / 1000,
                    "prefill_tok_s": row["mean_prefill_tok_s"],
                    "decode_tok_s": row["mean_decode_tok_s"],
                    "passed": row["passed"],
                    "attempted": row["attempted"],
                }
                for row in backend_summary
            },
            "pilot_ttft_s": {"first": ttft[0], "last": ttft[-1], "max": max(ttft), "mean": sum(ttft) / len(ttft)},
        },
        "quality_flags": {
            "runaway_generations": len(runaway),
            "runaway_token_cap": 2048,
        },
    }
    (output / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {output / 'metrics.json'}")
    return metrics


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pilot", required=True, type=Path)
    parser.add_argument("--benchmark", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    report = json.loads((args.pilot / "accuracy_report.json").read_text(encoding="utf-8"))
    rows = load_rows(args.pilot)

    figure_predictions(rows, args.output)
    specificity, sensitivity = figure_sensitivity(rows, args.output)
    figure_accuracy_by_level(report, args.output)
    backend_summary = figure_backends(args.benchmark, args.output)
    ttft = figure_ttft_drift(rows, args.output)
    figure_parser_fix(report, args.output)
    write_metrics(report, rows, backend_summary, ttft, specificity, sensitivity, args.output)


if __name__ == "__main__":
    main()
