"""Offline checks for scoring finished two-stage runs, drift detection, and figure output.

The runs here are made-up data with designed behaviour (a known slowdown, known accuracy),
used only to check the arithmetic and that every figure renders. Nothing here is a result.
"""

import contextlib
import io
import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import analyze_two_stage as analyze
from analyze_two_stage import describe, find_stabilization, rate, render_thermal_lines
from tamper_dataset import select_records


def make_manifest(pairs: int = 40, seed: int = 5) -> dict:
    rng = random.Random(seed)
    records = []
    kinds, edits = ("nano_banana", "qwen_inpaint"), ("add", "remove", "replace", "occlude", "resize")
    for n in range(pairs):
        x0, y0 = rng.uniform(0.05, 0.5), rng.uniform(0.05, 0.5)
        box = [round(x0, 3), round(y0, 3), round(x0 + rng.uniform(0.15, 0.4), 3), round(y0 + rng.uniform(0.15, 0.4), 3)]
        for variant, label in (("original", "real"), ("tampered", "tampered")):
            tampered = variant == "tampered"
            records.append({
                "pair_id": f"I{n:03d}", "variant": variant, "expected_label": label, "category": "Indoor",
                "source_kind": kinds[n % 2] if tampered else None, "edit": edits[n % 5] if tampered else None,
                "remote_filename": f"img-{n:03d}{variant[0]}.png", "prepared": f"prepared_768/img-{n:03d}{variant[0]}.png",
                "prepared_size": [576, 768], "gt_box_norm": box if tampered else None,
                "gt_quality": {"low_confidence": n == 3} if tampered else None,
            })
    return {"kind": "tamper_comparison", "records": records, "pairs": sorted({r["pair_id"] for r in records})}


def stage_result(response: str, ttft_s: float, tokens: int, tok_s: float, prompt_tokens: int, stop: str = "eos") -> dict:
    return {"status": "completed", "response": response, "ttft_ms": ttft_s * 1000, "generated_tokens": tokens,
            "decode_tok_s": tok_s, "decode_time_ms": tokens / tok_s * 1000, "prompt_tokens": prompt_tokens, "stop_reason": stop}


def make_synthetic_run(directory: Path, label: str, records: list[dict], runtime: str = "llama_cpp", slow: float = 0.4,
                       ramp: int = 30, sensitivity_every: int = 4, scale: str = "absolute", speed: float = 20.0,
                       seed: int = 0) -> None:
    """Write a run folder the way run_two_stage_series.py does. Tampered images 1 in `sensitivity_every` are missed."""
    rng = random.Random(seed)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "run-settings.json").write_text(json.dumps({
        "label": label, "model": f"{label} model", "runtime": runtime, "backend": "NPU",
        "device": {"model": "TestPhone", "soc": "SM8850"}}), encoding="utf-8")
    lines, thermal, host = [], [], 0.0
    miss = 0
    for position, record in enumerate(records, start=1):
        heat = 1 + slow * min(1.0, position / ramp)
        tampered = record["expected_label"] == "tampered"
        flagged = tampered and position % sensitivity_every != 0
        classification = "tampered" if flagged else "real"
        objects = [{"object": "thing", "edit": record["edit"], "location": "middle"}] if flagged else []
        stage1 = json.dumps({"explanation": "Looks edited." if flagged else "Nothing odd.", "classification": classification,
                             "tampered_objects": objects})
        tokens1 = int(rng.gauss(90, 8))
        if flagged:
            gt = record["gt_box_norm"]
            jitter = [rng.uniform(-0.02, 0.02) for _ in range(4)]
            unit = [g + j for g, j in zip(gt, jitter)]
            width, height = record["prepared_size"]
            scale_x = {"absolute": (width, height, width, height), "norm1000": (1000,) * 4}[scale]
            box = [round(u * s) for u, s in zip(unit, scale_x)]
            stage2 = json.dumps({"boxes": [{"object": "thing", "bbox": box}]})
            tokens2 = 40
        else:
            stage2, tokens2 = '{"boxes": []}', 6
        stage1_result = stage_result(stage1, 6.0 * heat * rng.uniform(0.97, 1.03), tokens1, speed / heat, 330)
        stage2_result = stage_result(stage2, 0.8 * heat, tokens2, speed / heat, 450 if flagged else 420)
        temperature = 28 + 9 * min(1.0, position / ramp)
        host += stage1_result["ttft_ms"] / 1000 + stage1_result["decode_time_ms"] / 1000
        lines.append(json.dumps({
            "iteration": position, "record_id": record["remote_filename"], "pair_id": record["pair_id"],
            "variant": record["variant"], "stage1": stage1_result, "stage2": stage2_result,
            "battery_c_before": temperature - 0.2, "battery_c_after": round(temperature, 1), "seen_s": round(host + position * 5, 1)}))
        thermal.append(json.dumps({"t_s": position * 50, "battery_c": round(temperature, 1), "thermal_status": 0 if temperature < 35 else 1}))
        miss += tampered and not flagged
    (directory / "iterations.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (directory / "thermal.jsonl").write_text("\n".join(thermal) + "\n", encoding="utf-8")
    (directory / "summary.json").write_text(json.dumps({"host_total_s": round(host * 1.1 + 600, 1), "instrumentation_ok": True}), encoding="utf-8")


class StabilizationTests(unittest.TestCase):
    def ramp(self, rise_over=30, total=80, low=10.0, high=14.0, noise=0.1, seed=1):
        rng = random.Random(seed)
        return [low + (high - low) * min(1.0, i / rise_over) + rng.uniform(-noise, noise) * low for i in range(total)]

    def test_finds_the_knee_of_a_ramp_that_levels_off(self):
        result = find_stabilization(self.ramp())
        self.assertTrue(result["drifted"])
        self.assertAlmostEqual(result["steady"], 14.0, delta=0.5)
        self.assertAlmostEqual(result["change_pct"], 40, delta=10)
        self.assertTrue(20 <= result["iteration"] <= 36, result["iteration"])

    def test_a_flat_series_reports_no_drift_from_the_start(self):
        result = find_stabilization(self.ramp(low=10.0, high=10.0))
        self.assertFalse(result["drifted"])
        self.assertEqual(result["iteration"], 1)

    def test_a_series_that_never_levels_off_is_stable_only_at_the_very_end(self):
        result = find_stabilization([10 + i * 0.5 for i in range(60)])
        self.assertTrue(result["drifted"])
        self.assertGreater(result["iteration"], 45)

    def test_a_plateau_needs_more_than_the_last_few_images(self):
        rising = find_stabilization([10 + i * 0.5 for i in range(60)])
        self.assertFalse(rising["plateau_reached"])
        self.assertLess(rising["held_images"], 15)
        leveled = find_stabilization(self.ramp())
        self.assertTrue(leveled["plateau_reached"])
        self.assertGreaterEqual(leveled["held_images"], 20)
        self.assertTrue(find_stabilization(self.ramp(low=10.0, high=10.0))["plateau_reached"])

    def test_report_says_no_plateau_instead_of_naming_a_level_off_image(self):
        with tempfile.TemporaryDirectory() as folder:
            rising = {"iteration": 49, "steady": 4.7, "initial": 3.9, "change_pct": 20.0, "drifted": True, "held_images": 2,
                      "plateau_reached": False, "window": 7, "tolerance": 0.1, "points": 50}
            metrics = {"label": "qairt", "stabilization": {"stage1_s": rising, "stage1_ms_per_token": None, "per_image_s": None},
                       "temperature_c": {"first": 40.0, "last": 41.0, "max": 41.0}}
            text = "\n".join(render_thermal_lines([metrics]))
        self.assertIn("no plateau is shown", text)
        self.assertNotIn("held within 10% from image 49", text)

    def test_too_few_points_gives_no_answer(self):
        self.assertIsNone(find_stabilization([1.0] * 10))
        self.assertIsNone(find_stabilization([None] * 50))

    def test_missing_values_are_skipped(self):
        values = self.ramp()
        values[10] = None
        self.assertIsNotNone(find_stabilization(values))


class HelperTests(unittest.TestCase):
    def test_describe_and_rate_ignore_missing_values(self):
        stats = describe([1, 2, 3, None, True, float("nan")])
        self.assertEqual((stats["n"], stats["mean"], stats["median"], stats["max"]), (3, 2.0, 2.0, 3.0))
        self.assertIsNone(describe([])["mean"])
        self.assertEqual(rate([True, False, None, True]), 2 / 3)
        self.assertIsNone(rate([None]))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manifest = make_manifest()
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.records = select_records(self.manifest, 60, seed=2)

    def run_cli(self, *argv):
        with patch.object(sys, "argv", ["analyze_two_stage.py", *argv]), contextlib.redirect_stdout(io.StringIO()):
            return analyze.main()

    def test_metrics_match_the_designed_behaviour(self):
        make_synthetic_run(self.root / "a", "alpha", self.records)
        out = self.root / "out"
        self.assertEqual(self.run_cli("--manifest", str(self.manifest_path), "--run", f"alpha={self.root / 'a'}",
                                      "--output", str(out), "--no-figures"), 0)
        m = json.loads((out / "metrics.json").read_text(encoding="utf-8"))["runs"][0]
        self.assertEqual((m["images"], m["tampered"], m["real"]), (60, 30, 30))
        self.assertEqual(m["verdict"]["specificity"], 1.0)
        self.assertAlmostEqual(m["verdict"]["sensitivity"], 0.75, delta=0.1)  # every 4th position missed, in run order
        # A missed image scores 0, so mean IoU = hit rate x mean IoU of hits. Dividing out the hit rate
        # checks box quality on its own, whichever images the order happens to make misses.
        self.assertGreater(m["boxes"]["mean_best_iou"] / m["boxes"]["hit_at_50"], 0.75)
        self.assertEqual(m["boxes"]["false_positive_on_real"], 0.0)
        self.assertEqual(m["boxes"]["excluded_low_confidence"], 1)  # pair I003's mask is flagged
        self.assertGreater(m["tokens"]["stage2_generated_tampered"], m["tokens"]["stage2_generated_real"])
        # The designed slowdown (+40%) finishes at image 30. Stage 1 time and decode ms per token find it;
        # the both-stage total is reported too but varies with how many stage 2 answers draw boxes.
        for series in ("stage1_s", "stage1_ms_per_token"):
            found = m["stabilization"][series]
            self.assertTrue(found["drifted"], series)
            self.assertTrue(15 <= found["iteration"] <= 35, (series, found["iteration"]))
            self.assertAlmostEqual(found["change_pct"], 40, delta=12, msg=series)
        self.assertIsNotNone(m["stabilization"]["per_image_s"])
        self.assertGreater(m["temperature_c"]["max"], m["temperature_c"]["first"])
        self.assertEqual(m["device"]["soc"], "SM8850")
        self.assertTrue((out / "scored_alpha.csv").is_file())
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("Thermal drift", report)
        self.assertIn("held within", report)

    def test_calibration_reads_a_zero_to_1000_answer_correctly(self):
        make_synthetic_run(self.root / "a", "pixels", self.records, scale="absolute")
        make_synthetic_run(self.root / "b", "thousand", self.records, runtime="qairt", scale="norm1000", seed=3)
        out = self.root / "out"
        self.run_cli("--manifest", str(self.manifest_path), "--run", f"pixels={self.root / 'a'}", "--run",
                     f"thousand={self.root / 'b'}", "--output", str(out), "--no-figures")
        runs = {m["label"]: m for m in json.loads((out / "metrics.json").read_text(encoding="utf-8"))["runs"]}
        self.assertEqual(runs["pixels"]["box_convention"], "absolute")
        self.assertEqual(runs["thousand"]["box_convention"], "norm1000")
        self.assertGreater(runs["thousand"]["boxes"]["mean_best_iou"] / runs["thousand"]["boxes"]["hit_at_50"], 0.75)
        forced = self.root / "forced"
        self.run_cli("--manifest", str(self.manifest_path), "--run", f"thousand={self.root / 'b'}", "--output", str(forced),
                     "--no-figures", "--convention", "absolute")
        wrong = json.loads((forced / "metrics.json").read_text(encoding="utf-8"))["runs"][0]
        self.assertLess(wrong["boxes"]["mean_best_iou"], runs["thousand"]["boxes"]["mean_best_iou"])

    def test_every_figure_renders_for_two_runs(self):
        make_synthetic_run(self.root / "a", "llama_cpp-npu", self.records)
        make_synthetic_run(self.root / "b", "qairt", self.records, runtime="qairt", slow=0.15, speed=14.0, seed=4)
        out = self.root / "out"
        self.run_cli("--manifest", str(self.manifest_path), "--run", f"llama_cpp-npu={self.root / 'a'}", "--run",
                     f"qairt={self.root / 'b'}", "--output", str(out))
        expected = {"timing_summary_llama_cpp-npu.png", "thermal_llama_cpp-npu.png", "timing_summary_qairt.png", "thermal_qairt.png",
                    "runtime_comparison.png", "time_comparison.png", "verdict_accuracy.png", "bbox_accuracy.png", "tokens_per_output.png"}
        self.assertEqual({p.name for p in out.glob("*.png")}, expected)
        for name in expected:
            with Image.open(out / name) as image:
                image.load()
                self.assertGreater(image.size[0], 1000, name)
        self.run_cli("--manifest", str(self.manifest_path), "--run", f"qairt={self.root / 'b'}", "--output", str(self.root / "dark"),
                     "--theme", "dark")
        self.assertTrue((self.root / "dark" / "thermal_qairt.png").is_file())

    def test_bad_arguments_are_rejected(self):
        make_synthetic_run(self.root / "a", "a", self.records)
        base = ["--manifest", str(self.manifest_path), "--output", str(self.root / "o"), "--no-figures"]
        for runs in ([f"{n}={self.root / 'a'}" for n in "wxyz"], [f"a={self.root / 'a'}", f"a={self.root / 'a'}"], ["nofolder"]):
            argv = base + [part for run in runs for part in ("--run", run)]
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_cli(*argv)

    def test_a_run_with_images_missing_from_the_manifest_is_refused(self):
        make_synthetic_run(self.root / "a", "a", self.records)
        small = {**self.manifest, "records": self.manifest["records"][:10]}
        self.manifest_path.write_text(json.dumps(small), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.run_cli("--manifest", str(self.manifest_path), "--run", f"a={self.root / 'a'}", "--output", str(self.root / "o"),
                         "--no-figures")


if __name__ == "__main__":
    unittest.main()
