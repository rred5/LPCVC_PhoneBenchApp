"""Figures for analyze_two_stage.py.

Colors come from the validated reference palette: stage 1 is blue and stage 2 orange in every
single-run figure; runs take blue, orange, aqua in the order they were given. Runs also differ
by marker shape, and filled versus hollow markers separate tampered from real images, so no
meaning rides on color alone. Gridlines are solid hairlines, each figure has a legend where
it has two or more series, and temperature gets its own panel rather than a second y-axis.
Legends sit in reserved headroom or the figure header so they never cover data. Needs numpy
and matplotlib.
"""

from pathlib import Path
import statistics

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
from matplotlib.ticker import PercentFormatter  # noqa: E402
import numpy as np  # noqa: E402


THEMES = {
    "light": {"surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
              "axis": "#c3c2b7", "stage": ("#2a78d6", "#eb6834"), "runs": ("#2a78d6", "#eb6834", "#1baf7a")},
    "dark": {"surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
             "axis": "#383835", "stage": ("#3987e5", "#d95926"), "runs": ("#3987e5", "#d95926", "#199e70")},
}
MARKERS = ("o", "s", "D")
DOT = 22  # marker area in points^2, about 9 px at 150 dpi
LINE = 1.2
SMOOTH = 7
HEADROOM = 0.2  # share of the y-range kept free above the data for the legend


def _theme(name: str) -> dict:
    t = THEMES[name]
    plt.rcParams.update({
        "figure.facecolor": t["surface"], "axes.facecolor": t["surface"], "savefig.facecolor": t["surface"],
        "text.color": t["ink"], "axes.labelcolor": t["ink2"], "xtick.color": t["muted"], "ytick.color": t["muted"],
        "axes.edgecolor": t["axis"], "axes.spines.top": False, "axes.spines.right": False,
        "axes.grid": True, "axes.grid.axis": "y", "grid.color": t["grid"], "grid.linewidth": 0.7, "grid.linestyle": "-",
        "axes.axisbelow": True, "font.family": "sans-serif",
        "font.sans-serif": ["Segoe UI", "Helvetica", "Arial", "DejaVu Sans"], "font.size": 10,
        "axes.titlesize": 11, "axes.titleweight": "bold", "axes.titlelocation": "left", "axes.titlepad": 8,
        "legend.frameon": False, "legend.fontsize": 9, "savefig.dpi": 150,
    })
    return t


def _array(values) -> np.ndarray:
    return np.array([np.nan if v is None else float(v) for v in values], dtype=float)


def _rolling(values: np.ndarray, window: int = SMOOTH) -> np.ndarray:
    out = np.full(len(values), np.nan)
    for index in range(window - 1, len(values)):
        chunk = values[index - window + 1:index + 1]
        chunk = chunk[~np.isnan(chunk)]
        if len(chunk) >= max(2, window // 2):
            out[index] = statistics.median(chunk)
    return out


def _headline(fig, t: dict, title: str, subtitle: str) -> None:
    fig.text(0.012, 0.985, title, ha="left", va="top", fontsize=14, fontweight="bold", color=t["ink"])
    fig.text(0.012, 0.94, subtitle, ha="left", va="top", fontsize=9.5, color=t["muted"])


def _empty(ax, t: dict, message: str = "no data") -> None:
    ax.text(0.5, 0.5, message, ha="center", va="center", color=t["muted"], transform=ax.transAxes)
    ax.set_xticks([])
    ax.set_yticks([])


def _headroom(ax, fraction: float = HEADROOM) -> None:
    low, high = ax.get_ylim()
    ax.set_ylim(low, high + (high - low) * fraction)


def _subtitle(metrics: dict) -> str:
    device = metrics.get("device") or {}
    phone = " ".join(str(device[k]) for k in ("model", "soc") if device.get(k))
    return f"{metrics['model']}  |  {metrics['runtime']} on {metrics['backend']}" + (f"  |  {phone}" if phone else "") \
        + f"  |  {metrics['images']} images ({metrics['tampered']} tampered, {metrics['real']} real)"


def _duration(seconds) -> str:
    if seconds is None:
        return "n/a"
    minutes, rest = divmod(round(seconds), 60)
    return f"{minutes}m {rest:02d}s"


def _save(fig, path: Path) -> str:
    fig.savefig(path)
    plt.close(fig)
    return path.name


def _dots(ax, x, y, tampered, color, t, marker="o") -> None:
    """Filled markers for tampered images, hollow for real ones."""
    x, y, tampered = np.asarray(x), np.asarray(y), np.asarray(tampered, dtype=bool)
    ax.scatter(x[tampered], y[tampered], s=DOT, color=color, marker=marker, zorder=3, linewidths=0)
    ax.scatter(x[~tampered], y[~tampered], s=DOT, facecolors=t["surface"], edgecolors=color, marker=marker,
               linewidths=1.2, zorder=3)


def _label_key(t: dict) -> list:
    return [Line2D([], [], marker="o", color=t["ink2"], linestyle="", markersize=6, label="tampered image"),
            Line2D([], [], marker="o", markerfacecolor=t["surface"], markeredgecolor=t["ink2"], color=t["ink2"],
                   linestyle="", markersize=6, label="real image")]


def _run_key(runs: list[str], t: dict) -> list:
    return [Line2D([], [], marker=MARKERS[i], color=t["runs"][i], linestyle="", markersize=7, label=name) for i, name in enumerate(runs)]


def _figure_legend(fig, handles: list) -> None:
    fig.legend(handles=handles, loc="upper right", bbox_to_anchor=(0.985, 0.985), ncol=len(handles))


def _strip(ax, positions, groups, color, t, marker, rng) -> None:
    """Every value as a point, jittered sideways, with a short bar at the group's median.

    Points rather than a box: many of these distributions are two clumps (a box found or
    missed, a stage 2 answer empty or full), and a box plot would shade the gap between them.
    """
    for position, group in zip(positions, groups):
        values = np.asarray([v for v in group if v is not None and np.isfinite(v)], dtype=float)
        if not len(values):
            continue
        ax.scatter(position + rng.uniform(-0.07, 0.07, len(values)), values, s=DOT * 0.7, color=color, marker=marker,
                   alpha=0.6, linewidths=0, zorder=2)
        ax.plot([position - 0.1, position + 0.1], [np.median(values)] * 2, color=t["ink"], lw=2.2, solid_capstyle="butt", zorder=4)


def timing_summary(scored: dict, metrics: dict, t: dict, path: Path) -> str:
    """Per-image time by stage, accumulated time, a stats table, and a histogram (one run)."""
    rows = scored["rows"]
    x = np.array([r["iteration"] for r in rows])
    tampered = [r["expected_label"] == "tampered" for r in rows]
    s1, s2 = _array([r["stage1_inference_s"] for r in rows]), _array([r["stage2_inference_s"] for r in rows])
    blue, orange = t["stage"]
    fig = plt.figure(figsize=(13, 7.6))
    grid = GridSpec(2, 2, figure=fig, width_ratios=[2.1, 1], left=0.06, right=0.985, top=0.85, bottom=0.08,
                    hspace=0.42, wspace=0.2)
    mean_total = metrics["timing"]["per_image_s"]["mean"]
    _headline(fig, t, f"{metrics['label']}: {mean_total:.1f} s of inference per image" if mean_total else f"{metrics['label']}: timing",
              _subtitle(metrics))

    ax = fig.add_subplot(grid[0, 0])
    for values, color in ((s1, blue), (s2, orange)):
        ax.plot(x, values, color=color, lw=LINE)
        _dots(ax, x, values, tampered, color, t)
    stable = metrics["stabilization"]["stage1_s"]
    handles = [Line2D([], [], color=blue, lw=2, label="Stage 1"), Line2D([], [], color=orange, lw=2, label="Stage 2"), *_label_key(t)]
    if stable and stable["drifted"] and stable["plateau_reached"]:
        ax.axvline(stable["iteration"], color=t["muted"], lw=1)
        handles.append(Line2D([], [], color=t["muted"], lw=1, label=f"stage 1 levels off: image {stable['iteration']}"))
    ax.set_title("Inference time per image")
    ax.set_ylabel("seconds")
    ax.set_xlabel("Nth image in run order")
    ax.set_xlim(0, len(rows) + 1)
    _headroom(ax, 0.22)
    ax.legend(handles=handles, loc="upper left", ncol=3)

    ax = fig.add_subplot(grid[1, 0])
    ax.plot(x, np.nancumsum(s1), color=blue, lw=LINE + 0.4, label="Stage 1")
    ax.plot(x, np.nancumsum(s2), color=orange, lw=LINE + 0.4, label="Stage 2")
    ax.plot(x, np.nancumsum(np.nan_to_num(s1) + np.nan_to_num(s2)), color=t["ink2"], lw=LINE + 0.4, label="Both stages")
    ax.set_title("Accumulated inference time")
    ax.set_ylabel("seconds")
    ax.set_xlabel("Nth image in run order")
    ax.set_xlim(0, len(rows) + 1)
    ax.legend(loc="upper left")

    ax = fig.add_subplot(grid[0, 1])
    ax.axis("off")
    timing = metrics["timing"]
    entries = [("Images", f"{metrics['tampered']} tampered, {metrics['real']} real"),
               ("Stage 1 average", f"{timing['stage1_s']['mean']:.2f} s" if timing["stage1_s"]["n"] else "n/a"),
               ("Stage 2 average", f"{timing['stage2_s']['mean']:.2f} s" if timing["stage2_s"]["n"] else "n/a"),
               ("Average per image", f"{timing['per_image_s']['mean']:.2f} s" if timing["per_image_s"]["n"] else "n/a"),
               ("Total, board", _duration(timing["board_total_s"])),
               ("Total, host", _duration(timing["host_total_s"] or timing["host_span_s"]))]
    ax.text(0, 1.0, "Timing summary", fontsize=11, fontweight="bold", va="top", transform=ax.transAxes)
    for index, (name, value) in enumerate(entries):
        y = 0.86 - index * 0.14
        ax.text(0, y, name, color=t["ink2"], va="center", transform=ax.transAxes)
        ax.text(1, y, value, color=t["ink"], va="center", ha="right", transform=ax.transAxes)
        ax.plot([0, 1], [y - 0.07, y - 0.07], color=t["grid"], lw=0.7, transform=ax.transAxes, clip_on=False)

    ax = fig.add_subplot(grid[1, 1])
    finite = np.concatenate([s1[~np.isnan(s1)], s2[~np.isnan(s2)]])
    if len(finite):
        edges = np.linspace(0, finite.max() * 1.02, 17)
        centers, width = (edges[:-1] + edges[1:]) / 2, (edges[1] - edges[0]) * 0.42
        for values, color, shift, name in ((s1, blue, -1, "Stage 1"), (s2, orange, 1, "Stage 2")):
            counts, _ = np.histogram(values[~np.isnan(values)], bins=edges)
            ax.bar(centers + shift * width / 2, counts, width, color=color, edgecolor=t["surface"], linewidth=1.5, label=name)
        ax.set_xlabel("seconds per stage")
        ax.set_ylabel("images")
        _headroom(ax, 0.2)
        ax.legend(loc="upper center", ncol=2)
    else:
        _empty(ax, t)
    ax.set_title("Time per stage")
    return _save(fig, path)


def thermal(scored: dict, metrics: dict, t: dict, path: Path) -> str:
    """Does the phone slow down as it warms? Stage 1 time, decode speed per token, temperature."""
    rows = scored["rows"]
    x = np.array([r["iteration"] for r in rows])
    tampered = [r["expected_label"] == "tampered" for r in rows]
    blue, orange = t["stage"]
    stage1 = _array([r["stage1_inference_s"] for r in rows])
    stable = metrics["stabilization"]["stage1_s"]
    per_token = metrics["stabilization"]["stage1_ms_per_token"]
    fig = plt.figure(figsize=(11, 9.2))
    grid = GridSpec(3, 1, figure=fig, left=0.08, right=0.97, top=0.87, bottom=0.07, hspace=0.45, height_ratios=[1.3, 1, 0.8])
    if stable is None:
        headline = f"{metrics['label']}: too few images to judge thermal drift"
    elif stable["drifted"] and per_token is not None and not per_token["drifted"]:
        headline = (f"{metrics['label']}: stage 1 time varies with answer length, but decode time per token held at about "
                    f"{per_token['steady']:.0f} ms, so no slowdown from heat in {len(rows)} images")
    elif stable["drifted"] and not stable["plateau_reached"]:
        headline = (f"{metrics['label']}: stage 1 time moved {stable['change_pct']:+.0f}% and was still moving when the "
                    f"run ended ({len(rows)} images); no plateau shown")
    elif stable["drifted"]:
        headline = (f"{metrics['label']}: stage 1 time moved {stable['change_pct']:+.0f}% and levels off near image "
                    f"{stable['iteration']}, at about {stable['steady']:.1f} s")
    else:
        headline = f"{metrics['label']}: no thermal drift beyond {stable['tolerance'] * 100:.0f}% in {len(rows)} images"
    _headline(fig, t, headline, _subtitle(metrics))

    ax = fig.add_subplot(grid[0])
    _dots(ax, x, stage1, tampered, blue, t)
    ax.plot(x, _rolling(stage1), color=t["ink"], lw=LINE + 0.8)
    handles = [Line2D([], [], color=t["ink"], lw=2, label=f"rolling median of {SMOOTH}"), *_label_key(t)]
    if stable:
        ax.axhspan(stable["steady"] * (1 - stable["tolerance"]), stable["steady"] * (1 + stable["tolerance"]),
                   color=t["muted"], alpha=0.14, lw=0)
        handles.append(Patch(facecolor=t["muted"], alpha=0.3, label=f"steady level {stable['steady']:.0f} s, +/-{stable['tolerance'] * 100:.0f}%"))
        if stable["drifted"] and stable["plateau_reached"]:
            ax.axvline(stable["iteration"], color=t["muted"], lw=1)
            handles.append(Line2D([], [], color=t["muted"], lw=1, label=f"levels off: image {stable['iteration']}"))
    ax.set_title("Stage 1 inference time per image")
    ax.set_ylabel("seconds")
    _headroom(ax, 0.22)
    ax.legend(handles=handles, loc="upper left", ncol=3)

    ax = fig.add_subplot(grid[1], sharex=ax)
    plotted = False
    for stage, color, name in (("stage1", blue, "Stage 1"), ("stage2", orange, "Stage 2")):
        values = _array([r[f"{stage}_ms_per_token"] for r in rows])
        if np.isfinite(values).sum() >= 2:
            ax.scatter(x, values, s=DOT * 0.6, color=color, alpha=0.35, linewidths=0, zorder=2)
            ax.plot(x, _rolling(values), color=color, lw=LINE + 0.6, label=name)
            plotted = True
    if plotted:
        _headroom(ax, 0.2)
        ax.legend(loc="upper left", ncol=2)
    else:
        _empty(ax, t)
    ax.set_title("Decode time per generated token (not affected by how long the answer is)")
    ax.set_ylabel("ms per token")

    ax = fig.add_subplot(grid[2], sharex=ax)
    temperature = _array([r["battery_c_after"] for r in rows])
    if np.isfinite(temperature).any():
        ax.plot(x, temperature, color=t["ink2"], lw=LINE + 0.4)
        ax.scatter(x, temperature, s=DOT * 0.7, color=t["ink2"], linewidths=0, zorder=3)
    else:
        _empty(ax, t, "no temperature readings in this run")
    ax.set_title("Battery temperature")
    ax.set_ylabel("deg C")
    ax.set_xlabel("Nth image in run order")
    ax.set_xlim(0, len(rows) + 1)
    return _save(fig, path)


def _comparison_axes(ncols: int, nrows: int = 1, width: float = 13, height: float = 5.6, **grid):
    spec = {"left": 0.06, "right": 0.985, "top": 0.8, "bottom": 0.12, "wspace": 0.2, **grid}
    return plt.subplots(nrows, ncols, figsize=(width, height), gridspec_kw=spec)


def runtime_comparison(scored_runs: list[dict], metrics: list[dict], t: dict, path: Path) -> str:
    """Time to first token, decode speed and answer length for each stage, run against run."""
    labels = [m["label"] for m in metrics]
    panels = [("Stage 1: time to first token", "stage1_ttft_s", "seconds"), ("Stage 1: decode speed", "stage1_decode_tok_s", "tokens/s"),
              ("Stage 1: tokens generated", "stage1_generated_tokens", "tokens"),
              ("Stage 2: time to first token", "stage2_ttft_s", "seconds"), ("Stage 2: decode speed", "stage2_decode_tok_s", "tokens/s"),
              ("Stage 2: tokens generated", "stage2_generated_tokens", "tokens")]
    fig, axes = _comparison_axes(3, 2, height=7.4, top=0.84, bottom=0.09, hspace=0.4, wspace=0.28)
    rng = np.random.default_rng(0)
    _headline(fig, t, "Runtime comparison: same prompts, same images", " vs ".join(f"{m['label']} ({m['model']})" for m in metrics))
    _figure_legend(fig, _run_key(labels, t))
    for ax, (title, key, unit) in zip(axes.flat, panels):
        for index, scored in enumerate(scored_runs):
            _strip(ax, [index], [[r[key] for r in scored["rows"]]], t["runs"][index], t, MARKERS[index], rng)
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels)
        ax.set_xlim(-0.6, len(labels) - 0.4)
        ax.set_ylim(bottom=0)  # a run-against-run comparison must not start above zero
        ax.set_title(title)
        ax.set_ylabel(unit)
    return _save(fig, path)


def time_comparison(scored_runs: list[dict], metrics: list[dict], t: dict, path: Path) -> str:
    """Stage 1 time per image and decode cost per token against run order, one line per run."""
    labels = [m["label"] for m in metrics]
    fig, axes = _comparison_axes(2, wspace=0.2)
    _headline(fig, t, "Time against run order", "Dots are single images; lines are the rolling median of 7. Stage 1 does comparable work for every image.")
    _figure_legend(fig, _run_key(labels, t))
    for index, scored in enumerate(scored_runs):
        rows = scored["rows"]
        x = np.array([r["iteration"] for r in rows])
        for ax, key in zip(axes, ("stage1_inference_s", "stage1_ms_per_token")):
            values = _array([r[key] for r in rows])
            ax.scatter(x, values, s=DOT * 0.6, color=t["runs"][index], marker=MARKERS[index], alpha=0.35, linewidths=0, zorder=2)
            ax.plot(x, _rolling(values), color=t["runs"][index], lw=LINE + 0.8)
    axes[0].set_title("Stage 1 inference time per image")
    axes[0].set_ylabel("seconds")
    axes[1].set_title("Stage 1 decode time per generated token")
    axes[1].set_ylabel("ms per token")
    for ax in axes:
        ax.set_xlabel("Nth image in run order")
    return _save(fig, path)


def _grouped_bars(ax, categories: list[str], series: list[list[float | None]], labels: list[str], t: dict) -> None:
    width = 0.8 / max(1, len(series))
    for index, values in enumerate(series):
        offsets = np.arange(len(categories)) + (index - (len(series) - 1) / 2) * width
        ax.bar(offsets, [0 if v is None else v for v in values], width * 0.9, color=t["runs"][index],
               edgecolor=t["surface"], linewidth=1.5, label=labels[index])
    ax.set_xticks(range(len(categories)))
    ax.set_xticklabels(categories)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.set_yticks([0, 0.2, 0.4, 0.6, 0.8, 1.0])
    ax.set_ylim(0, 1.04)


def verdict_accuracy(metrics: list[dict], t: dict, path: Path) -> str:
    labels = [m["label"] for m in metrics]
    fig, axes = _comparison_axes(2, wspace=0.18, width_ratios=[1, 1.25])
    _headline(fig, t, "Verdict accuracy", "Sensitivity is the share of tampered images the model flagged; specificity is real images it left alone")
    _figure_legend(fig, [Line2D([], [], marker="s", color=t["runs"][i], linestyle="", markersize=8, label=n) for i, n in enumerate(labels)])
    names = ["3-class", "Real vs\nmanipulated", "Sensitivity", "Specificity"]
    keys = ("three_class_accuracy", "binary_accuracy", "sensitivity", "specificity")
    _grouped_bars(axes[0], names, [[m["verdict"][k] for k in keys] for m in metrics], labels, t)
    axes[0].set_title("Overall")
    edits = sorted({e for m in metrics for e in m["verdict"]["sensitivity_by_edit"]})
    _grouped_bars(axes[1], edits or ["-"], [[m["verdict"]["sensitivity_by_edit"].get(e, {}).get("rate") for e in edits] for m in metrics], labels, t)
    axes[1].set_title("Sensitivity by edit type")
    return _save(fig, path)


def bbox_accuracy(scored_runs: list[dict], metrics: list[dict], t: dict, path: Path) -> str:
    labels = [m["label"] for m in metrics]
    fig, axes = _comparison_axes(2, wspace=0.18, width_ratios=[1.6, 1])
    counts = ", ".join(f"{m['label']} {m['boxes']['scored']}" for m in metrics)
    _headline(fig, t, "Bounding-box accuracy on tampered images",
              f"IoU of the best-matching box with the mask-derived box (1 is perfect); low-confidence masks are excluded. Images scored: {counts}")
    _figure_legend(fig, _run_key(labels, t))
    edits = sorted({r["edit"] for s in scored_runs for r in s["rows"] if r["box_scored"]})
    rng = np.random.default_rng(1)
    if edits:
        for index, scored in enumerate(scored_runs):
            offset = (index - (len(scored_runs) - 1) / 2) * 0.25
            _strip(axes[0], [i + offset for i in range(len(edits))],
                   [[r["best_iou"] for r in scored["rows"] if r["box_scored"] and r["edit"] == e] for e in edits],
                   t["runs"][index], t, MARKERS[index], rng)
        axes[0].set_xticks(range(len(edits)))
        axes[0].set_xticklabels(edits)
        axes[0].set_xlim(-0.6, len(edits) - 0.4)
        axes[0].set_ylim(-0.04, 1.04)
        axes[0].axhline(0.5, color=t["muted"], lw=0.9)
        axes[0].text(len(edits) - 0.45, 0.52, "IoU 0.5", color=t["muted"], ha="right", fontsize=8.5)
    else:
        _empty(axes[0], t, "no boxes to score")
    axes[0].set_title("Best IoU by edit type (bar = median)")
    axes[0].set_ylabel("IoU")
    _grouped_bars(axes[1], ["Hit at\nIoU 0.5", "Box centre\non target", "No box\nat all"],
                  [[m["boxes"]["hit_at_50"], m["boxes"]["center_hit"], m["boxes"]["no_box_on_tampered"]] for m in metrics], labels, t)
    axes[1].set_title("Share of scored tampered images")
    return _save(fig, path)


def tokens_per_output(scored_runs: list[dict], metrics: list[dict], t: dict, path: Path) -> str:
    labels = [m["label"] for m in metrics]
    fig, axes = _comparison_axes(2, wspace=0.18)
    _headline(fig, t, "Tokens generated per answer",
              "Stage 2 on a real image should be near-empty; a long stage 2 there means the model invented boxes (bar = median)")
    _figure_legend(fig, _run_key(labels, t))
    rng = np.random.default_rng(2)
    groups = ("real", "tampered")
    for ax, stage in zip(axes, ("stage1", "stage2")):
        for index, scored in enumerate(scored_runs):
            offset = (index - (len(scored_runs) - 1) / 2) * 0.25
            _strip(ax, [i + offset for i in range(len(groups))],
                   [[r[f"{stage}_generated_tokens"] for r in scored["rows"] if r["expected_label"] == g] for g in groups],
                   t["runs"][index], t, MARKERS[index], rng)
        ax.set_xticks(range(len(groups)))
        ax.set_xticklabels([f"{g} images" for g in groups])
        ax.set_xlim(-0.6, len(groups) - 0.4)
        ax.set_title(f"Stage {stage[-1]}")
        ax.set_ylabel("tokens")
    return _save(fig, path)


def make_figures(scored_runs: list[dict], metrics: list[dict], output: Path, theme: str = "light") -> list[str]:
    """Write every figure into `output`; returns the filenames."""
    names = []
    with plt.rc_context():
        t = _theme(theme)
        for scored, m in zip(scored_runs, metrics):
            names.append(timing_summary(scored, m, t, output / f"timing_summary_{m['label']}.png"))
            names.append(thermal(scored, m, t, output / f"thermal_{m['label']}.png"))
        if len(scored_runs) > 1:
            names.append(runtime_comparison(scored_runs, metrics, t, output / "runtime_comparison.png"))
            names.append(time_comparison(scored_runs, metrics, t, output / "time_comparison.png"))
        names.append(verdict_accuracy(metrics, t, output / "verdict_accuracy.png"))
        names.append(bbox_accuracy(scored_runs, metrics, t, output / "bbox_accuracy.png"))
        names.append(tokens_per_output(scored_runs, metrics, t, output / "tokens_per_output.png"))
    return names
