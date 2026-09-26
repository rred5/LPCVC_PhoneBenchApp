"""Offline regression tests for the accuracy-pilot parser and reports.

These tests deliberately avoid ADB, Android, and model execution.  They cover
the small pieces that determine whether a pilot result is counted and how its
denominators and summary statistics are reported.
"""

import json
from pathlib import Path
import statistics
import sys
import unittest
from unittest.mock import patch

import run_accuracy_pilot as pilot


METRICS = pilot.METRICS


def metric_row(values):
    """Create a complete metric row without any phone or runner state."""
    return dict(zip(METRICS, values))


def classified_row(response, expected):
    """Apply the same parser-to-counting rule used by ``run_image``."""
    predicted = pilot.strict_classification(response)
    return {
        **metric_row([1, 2, 3, 4, 5, 6]),
        "predicted_label": predicted,
        "classification_valid": predicted is not None,
        "classification_correct": predicted == expected if predicted is not None else False,
    }


class StrictClassificationTests(unittest.TestCase):
    def test_only_bare_single_key_object_is_accepted(self):
        self.assertEqual(
            pilot.strict_classification('{"classification":"tampered"}'),
            "tampered",
        )
        for response in (
            "",
            "not json",
            "```json\n{\"classification\":\"real\"}\n```",
            '{"classification":"real","explanation":"extra key"}',
            '{"classification":"unknown"}',
            '{"classification":null}',
            '[{"classification":"real"}]',
        ):
            with self.subTest(response=response):
                self.assertIsNone(pilot.strict_classification(response))

    def test_duplicate_classification_keys_are_rejected(self):
        # json.loads normally keeps only the last duplicate key.  A strict
        # benchmark parser must reject this ambiguous response instead.
        self.assertIsNone(
            pilot.strict_classification(
                '{"classification":"real","classification":"tampered"}'
            )
        )


class ClassificationReportTests(unittest.TestCase):
    def test_invalid_and_error_responses_are_attempts_but_never_correct(self):
        rows = [
            classified_row('{"classification":"real"}', "real"),
            classified_row('{"classification":"real"}', "tampered"),
            classified_row("model timeout: classification unavailable", "tampered"),
            classified_row('{"error":"generation failed","classification":"tampered"}', "tampered"),
        ]

        stats = pilot.classification_stats(rows, scheduled=4)

        self.assertEqual(stats["attempted"], 4)
        self.assertEqual(stats["valid"], 2)
        self.assertEqual(stats["invalid"], 2)
        self.assertEqual(stats["correct"], 1)
        self.assertEqual(stats["accuracy_all_attempts"], 1 / 4)
        self.assertEqual(stats["accuracy_valid_only"], 1 / 2)
        self.assertEqual(stats["prediction_counts"], {"invalid": 2, "real": 2})

    def test_missing_runs_count_as_failed_denominator(self):
        rows = [classified_row('{"classification":"real"}', "real"), classified_row("timeout", "real")]

        stats = pilot.classification_stats(rows, scheduled=5)

        self.assertEqual(stats["observed"], 2)
        self.assertEqual(stats["attempted"], 5)
        self.assertEqual(stats["failed_runs"], 3)
        self.assertEqual(stats["invalid"], 4)
        self.assertEqual(stats["correct"], 1)
        self.assertEqual(stats["accuracy_all_attempts"], 1 / 5)
        self.assertEqual(stats["accuracy_valid_only"], 1.0)
        self.assertEqual(stats["prediction_counts"], {"failed": 3, "invalid": 1, "real": 1})

    def test_partial_completed_report_uses_actual_attempts(self):
        # A configured repetition count must not inflate the denominator when
        # only a subset of runs produced records.
        partial = {
            "status": "partial",
            "difficulty": "original",
            "attempted": 2,
            "valid": 1,
            "invalid": 1,
            "correct": 1,
            "diagnostic_valid": 2,
            "diagnostic_correct": 1,
        }
        failed = {
            "status": "error",
            "difficulty": "original",
            "error": "transport disconnected before any result",
        }

        summary = pilot.aggregate_reports([partial, failed])

        self.assertEqual(summary["images"], 2)
        self.assertEqual(summary["completed_images"], 0)
        self.assertEqual(summary["failed_images"], 1)
        self.assertEqual(summary["partial_images"], 1)
        self.assertEqual(summary["overall"]["attempted"], 2)
        self.assertEqual(summary["overall"]["valid"], 1)
        self.assertEqual(summary["overall"]["invalid"], 1)
        self.assertEqual(summary["overall"]["correct"], 1)
        self.assertEqual(summary["overall"]["accuracy_all_attempts"], 1 / 2)
        self.assertEqual(summary["overall"]["accuracy_valid_only"], 1.0)


class NumericReportTests(unittest.TestCase):
    def test_numeric_stats_use_mean_and_sample_standard_deviation(self):
        rows = [
            metric_row([10, 20, 30, 40, 50, 60]),
            metric_row([20, 30, 40, 50, 60, 70]),
            metric_row([30, 40, 50, 60, 70, 80]),
        ]

        stats = pilot.numeric_stats(rows)

        for metric in METRICS:
            with self.subTest(metric=metric):
                self.assertEqual(stats[f"mean_{metric}"], 10 + rows[0][metric])
                self.assertEqual(stats[f"sd_{metric}"], statistics.stdev(row[metric] for row in rows))

    def test_numeric_stats_ignore_missing_metric_values(self):
        rows = [
            {"prompt_tokens": 10},
            {"prompt_tokens": None},
            {"prompt_tokens": 30},
        ]

        stats = pilot.numeric_stats(rows)

        self.assertEqual(stats["mean_prompt_tokens"], 20)
        self.assertEqual(stats["sd_prompt_tokens"], statistics.stdev([10, 30]))
        self.assertIsNone(stats["mean_generated_tokens"])
        self.assertIsNone(stats["sd_generated_tokens"])


class SettingsAndCliTests(unittest.TestCase):
    def test_default_settings_load_and_pair_selection_are_deterministic(self):
        settings = pilot.load_settings(Path(__file__).with_name("accuracy_pilot_defaults.json"))

        self.assertEqual(settings["schema_version"], 1)
        self.assertEqual(pilot.parse_pairs("2", ["I016", "L080", "L104"]), ["I016", "L080"])
        self.assertEqual(pilot.parse_pairs("L104,I016", ["I016", "L080", "L104"]), ["L104", "I016"])

    def test_settings_reject_unknown_metric(self):
        settings = json.loads(
            Path(__file__).with_name("accuracy_pilot_defaults.json").read_text(encoding="utf-8")
        )
        settings["output"]["metrics"].append("not_a_metric")

        # Patch the reader so this test stays entirely in memory on the
        # managed Windows workspace, where temporary-directory ACLs can vary.
        with patch.object(pilot, "read_json", return_value=settings):
            with self.assertRaisesRegex(ValueError, "unsupported metric"):
                pilot.load_settings(Path("settings-with-unknown-metric.json"))

    def test_explicit_zero_repetitions_are_rejected_instead_of_using_the_default(self):
        # ``args.repetitions or default`` silently turns an explicit zero into
        # the configured value.  The CLI should reject the invalid override.
        dataset = Path(__file__).parent
        fake_manifest = {"pairs": ["I016"], "records": []}
        argv = [
            "run_accuracy_pilot.py",
            "--dataset",
            str(dataset),
            "--pairs",
            "I016",
            "--output",
            str(dataset),
            "--repetitions",
            "0",
        ]
        with patch.object(pilot, "build_manifest", return_value=fake_manifest), patch.object(
            pilot, "write_json"
        ), patch.object(pilot, "write_report", return_value={}), patch.object(sys, "argv", argv):
            with self.assertRaises(SystemExit) as raised:
                pilot.main()

        self.assertEqual(raised.exception.code, 2)


class LenientClassificationTests(unittest.TestCase):
    """Response shapes taken verbatim from the 2026-09-20 NPU pilot."""

    def test_bare_label_ignores_case_and_punctuation(self):
        for text in ("Real", "real", "Real.", "**tampered**", "  Synthetic  "):
            with self.subTest(text=text):
                self.assertIn(pilot.lenient_classification(text), pilot.LABELS)

    def test_opening_verdict_wins_over_supporting_detail(self):
        response = (
            "This image is real.\n\nThe image appears to be a genuine photograph "
            "with natural textures and lighting, and it is not manipulated."
        )
        self.assertEqual(pilot.lenient_classification(response), "real")

    def test_negated_manipulation_reads_as_real(self):
        response = "There are no obvious signs of digital manipulation or synthetic artifacts."
        self.assertEqual(pilot.lenient_classification(response), "real")

    def test_negated_tampering_reads_as_real(self):
        response = "The object in the foreground is not tampered with."
        self.assertEqual(pilot.lenient_classification(response), "real")

    def test_closing_verdict_wins_when_model_reasons_first(self):
        response = (
            "Based on the provided image, I can provide the following analysis:\n"
            "- The image contains significant digital artifacts.\n"
            "- This further supports the conclusion that the image is not real.\n\n"
            "The image is **synthetic**."
        )
        self.assertEqual(pilot.lenient_classification(response), "synthetic")

    def test_closing_verdict_after_bulleted_reasoning(self):
        response = (
            "Analysis:\n- The lettering on the sign is slightly distorted.\n"
            "- The lighting and shadows are inconsistent.\n\n"
            "Based on the visual evidence, this image appears to be AI-generated "
            "or digitally manipulated."
        )
        self.assertEqual(pilot.lenient_classification(response), "synthetic")

    def test_hedged_verdict_takes_first_cue(self):
        self.assertEqual(
            pilot.lenient_classification("This image is likely synthetic or tampered."),
            "synthetic",
        )

    def test_unparseable_response_stays_none(self):
        for text in ("", "   ", None, "I am unable to help with that."):
            with self.subTest(text=text):
                self.assertIsNone(pilot.lenient_classification(text))


class BinaryScoringTests(unittest.TestCase):
    def test_synthetic_counts_as_manipulated_against_tampered_truth(self):
        fields = pilot.binary_fields("synthetic", "tampered")
        self.assertFalse(fields["diagnostic_correct"])
        self.assertTrue(fields["binary_correct"])

    def test_real_prediction_on_real_truth_is_correct_both_ways(self):
        fields = pilot.binary_fields("real", "real")
        self.assertTrue(fields["diagnostic_correct"])
        self.assertTrue(fields["binary_correct"])

    def test_unparsed_prediction_is_never_counted_correct(self):
        fields = pilot.binary_fields(None, "tampered")
        self.assertFalse(fields["diagnostic_valid"])
        self.assertFalse(fields["binary_correct"])
        self.assertIsNone(fields["binary_label"])


class ConsistencyReportingTests(unittest.TestCase):
    def test_all_unparsed_runs_do_not_report_full_agreement(self):
        rows = [
            {**metric_row([1, 2, 3, 4, 5, 6]), "diagnostic_label": None, "diagnostic_valid": False}
            for _ in range(3)
        ]
        stats = pilot.classification_stats(rows, scheduled=3)
        self.assertIsNone(stats["diagnostic_valid_prediction_consistency"])

    def test_agreement_is_measured_across_parsed_runs_only(self):
        labels = ["real", "real", "synthetic"]
        rows = [
            {**metric_row([1, 2, 3, 4, 5, 6]), "diagnostic_label": label, "diagnostic_valid": True}
            for label in labels
        ]
        stats = pilot.classification_stats(rows, scheduled=3)
        self.assertAlmostEqual(stats["diagnostic_valid_prediction_consistency"], 2 / 3)


if __name__ == "__main__":
    unittest.main()
