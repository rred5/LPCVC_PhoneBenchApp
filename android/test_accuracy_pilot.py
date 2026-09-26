"""Offline checks for the pilot's parsing, reporting, and stale-output guards."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from prepare_accuracy_dataset import prepare
from run_accuracy_pilot import (
    aggregate_reports,
    build_image_report,
    clear_remote_results,
    strict_classification,
)


class AccuracyPilotTests(unittest.TestCase):
    def test_strict_parser_rejects_prose_and_extra_keys(self):
        self.assertEqual(strict_classification('{"classification":"tampered"}'), "tampered")
        self.assertIsNone(strict_classification('Here is the JSON: {"classification":"tampered"}'))
        self.assertIsNone(strict_classification('```json\n{"classification":"tampered"}\n```'))
        self.assertIsNone(strict_classification('{"classification":"real","reason":"looks real"}'))
        self.assertIsNone(strict_classification('{"classification":"Real"}'))
        self.assertIsNone(strict_classification('{"classification":"real","classification":"tampered"}'))

    def test_partial_runs_count_missing_repetitions_as_invalid(self):
        record = {
            "pair_id": "X",
            "difficulty": "realistic",
            "expected_label": "tampered",
            "source": "raw/X.png",
            "prepared": "prepared_768/img.png",
            "remote_filename": "img.png",
        }
        row = {
            "prompt_tokens": 10,
            "generated_tokens": 3,
            "reported_total_tokens": 13,
            "ttft_ms": 100,
            "prefill_tok_s": 2,
            "decode_tok_s": 3,
            "response": '{"classification":"tampered"}',
            "classification_valid": True,
            "classification_correct": True,
            "predicted_label": "tampered",
        }
        report = build_image_report(record, [row], 1.0, scheduled=3, error="session ended")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["attempted"], 3)
        self.assertEqual(report["valid"], 1)
        self.assertEqual(report["invalid"], 2)
        self.assertAlmostEqual(report["accuracy_all_attempts"], 1 / 3)
        summary = aggregate_reports([report])
        self.assertEqual(summary["realistic"]["attempted"], 3)
        self.assertAlmostEqual(summary["realistic"]["accuracy_all_attempts"], 1 / 3)

    def test_prepared_collision_is_rejected(self):
        root = Path(__file__).resolve().parent
        source = root / "_accuracy_pilot_test_source.png"
        destination = root / "_accuracy_pilot_test_prepared.png"
        try:
            Image.new("RGB", (4, 2), "red").save(source)
            prepare(source, destination, 3)
            with Image.new("RGB", (1, 1), "blue") as wrong:
                wrong.save(destination)
            with self.assertRaises(RuntimeError):
                prepare(source, destination, 3)
        finally:
            source.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)

    def test_remote_checkpoint_cleanup_is_explicit(self):
        completed = __import__("subprocess").CompletedProcess([], 0, "", "")
        with patch("run_accuracy_pilot.run_command", return_value=completed) as mocked:
            clear_remote_results(3)
        command = mocked.call_args.args[0]
        self.assertEqual(command[:5], ["adb", "shell", "run-as", "com.geniex.demo", "rm"])
        self.assertEqual(command[-3:], [
            "files/benchmark/results/warm-NPU-1.json",
            "files/benchmark/results/warm-NPU-2.json",
            "files/benchmark/results/warm-NPU-3.json",
        ])


if __name__ == "__main__":
    unittest.main()
