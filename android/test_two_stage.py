"""Offline checks for two-stage prompt text, response parsing, box scoring, and timing."""

import json
from pathlib import Path
import re
import unittest

from two_stage import (
    calibrate_convention,
    detect_convention,
    extract_json,
    iou,
    load_prompts,
    normalize_label,
    parse_stage1,
    parse_stage2,
    render,
    score_boxes,
    score_iteration,
    stage_timing,
    to_unit_box,
)


DOC = Path(__file__).resolve().parent.parent / "docs" / "two_stage_prompt.md"
STAGE1_OK = json.dumps({
    "explanation": "The lamp's shadow points the wrong way.",
    "classification": "tampered",
    "tampered_objects": [{"object": "lamp", "edit": "add", "location": "top right"}],
})


class PromptTests(unittest.TestCase):
    def test_prompts_fit_the_server_limit_and_name_their_placeholders(self):
        prompts = load_prompts()
        for key in ("stage1", "stage2", "explain_variant"):
            self.assertLess(len(prompts[key]), 2000, key)
        rendered = render(prompts["stage2"], 576, 768)
        self.assertIn("576 pixels wide and 768 pixels tall", rendered)
        self.assertNotRegex(rendered, r"\{(width|height)\}")
        self.assertIn('{"boxes"', rendered)  # JSON braces survive rendering

    def test_documentation_shows_exactly_the_prompts_the_runner_sends(self):
        blocks = re.findall(r"```text\n(.*?)\n```", DOC.read_text(encoding="utf-8"), re.S)
        prompts = load_prompts()
        self.assertEqual(blocks, [prompts["stage1"], prompts["stage2"], prompts["explain_variant"]])


class Stage1Tests(unittest.TestCase):
    def test_strict_json_with_the_requested_fields_is_schema_ok(self):
        parsed = parse_stage1(STAGE1_OK)
        self.assertEqual((parsed["label"], parsed["parse_mode"], parsed["schema_ok"]), ("tampered", "strict", True))
        self.assertEqual(parsed["objects"], [{"object": "lamp", "edit": "add", "location": "top right"}])

    def test_fenced_json_is_strict_and_json_in_prose_is_embedded(self):
        self.assertEqual(parse_stage1(f"```json\n{STAGE1_OK}\n```")["parse_mode"], "strict")
        embedded = parse_stage1(f"Sure! Here is my answer: {STAGE1_OK} Hope that helps.")
        self.assertEqual((embedded["label"], embedded["parse_mode"], embedded["schema_ok"]), ("tampered", "embedded", False))

    def test_extra_or_missing_fields_are_valid_but_not_schema_ok(self):
        extra = parse_stage1('{"explanation": "x", "classification": "real", "tampered_objects": [], "confidence": 0.9}')
        self.assertEqual((extra["label"], extra["schema_ok"]), ("real", False))
        missing = parse_stage1('{"classification": "real"}')
        self.assertEqual((missing["label"], missing["schema_ok"]), ("real", False))

    def test_label_aliases_map_to_the_three_classes(self):
        for text, expected in {"real": "real", "Fully Fake": "fully_fake", "fully-fake": "fully_fake",
                               "synthetic": "fully_fake", "AI-generated": "fully_fake", "Tampered": "tampered"}.items():
            self.assertEqual(normalize_label(text), expected, text)
        self.assertIsNone(normalize_label("maybe"))
        self.assertIsNone(normalize_label(None))

    def test_prose_falls_back_to_the_lenient_parser_and_maps_synthetic(self):
        self.assertEqual(parse_stage1("This image appears to be a real photograph.")["parse_mode"], "prose")
        self.assertEqual(parse_stage1("This image is an AI-generated synthetic picture.")["label"], "fully_fake")
        nothing = parse_stage1("")
        self.assertEqual((nothing["label"], nothing["parse_mode"]), (None, "none"))

    def test_output_cut_off_mid_json_keeps_whatever_prose_survives(self):
        cut = '{"explanation": "The shadow on the table is missing, so this image is tampered because'
        parsed = parse_stage1(cut)
        self.assertEqual(parsed["parse_mode"], "prose")
        self.assertFalse(parsed["schema_ok"])

    def test_negated_verdict_is_not_read_as_tampered(self):
        self.assertEqual(parse_stage1("There is no sign this image is tampered. It looks real.")["label"], "real")


class Stage2Tests(unittest.TestCase):
    def test_json_boxes_with_label_and_alternative_keys(self):
        for key in ("bbox", "bbox_2d", "box"):
            parsed = parse_stage2(json.dumps({"boxes": [{"object": "lamp", key: [10, 20, 110, 220]}]}))
            self.assertEqual(parsed["boxes"], [{"object": "lamp", "box": [10.0, 20.0, 110.0, 220.0]}], key)
            self.assertEqual(parsed["parse_mode"], "strict")

    def test_empty_list_is_a_declared_answer_not_a_parse_failure(self):
        empty = parse_stage2('{"boxes": []}')
        self.assertEqual((empty["boxes"], empty["declared_empty"], empty["parse_mode"]), ([], True, "strict"))
        garbage = parse_stage2("I cannot determine that.")
        self.assertEqual((garbage["boxes"], garbage["declared_empty"], garbage["parse_mode"]), ([], False, "none"))

    def test_unusable_boxes_are_counted_not_scored(self):
        parsed = parse_stage2('{"boxes": [{"object": "a", "bbox": [50, 50, 10, 10]}, {"object": "b", "bbox": [1, 2, 3]},'
                              ' {"object": "c", "bbox": ["x", 1, 2, 3]}, {"object": "d", "bbox": [0, 0, 5, 5]}]}')
        self.assertEqual([entry["object"] for entry in parsed["boxes"]], ["d"])
        self.assertEqual(parsed["invalid_boxes"], 3)

    def test_numeric_strings_and_a_bare_box_are_accepted(self):
        self.assertEqual(parse_stage2('{"boxes": [{"bbox": ["10", "20", "30", "40"]}]}')["boxes"][0]["box"], [10.0, 20.0, 30.0, 40.0])
        self.assertEqual(parse_stage2("[10, 20, 30, 40]")["boxes"][0]["box"], [10.0, 20.0, 30.0, 40.0])

    def test_qwen_native_text_and_bracketed_prose(self):
        native = parse_stage2("The lamp is at <|box_start|>(100,200),(300,400)<|box_end|>.")
        self.assertEqual((native["boxes"][0]["box"], native["parse_mode"]), ([100.0, 200.0, 300.0, 400.0], "prose"))
        bracket = parse_stage2("Box: [12, 34, 56, 78]")
        self.assertEqual(bracket["boxes"][0]["box"], [12.0, 34.0, 56.0, 78.0])


class GeometryTests(unittest.TestCase):
    def test_iou_values(self):
        self.assertEqual(iou([0, 0, 1, 1], [0, 0, 1, 1]), 1.0)
        self.assertEqual(iou([0, 0, 0.5, 1], [0.5, 0, 1, 1]), 0.0)
        self.assertAlmostEqual(iou([0, 0, 0.5, 0.5], [0.25, 0.25, 0.75, 0.75]), 0.0625 / 0.4375)

    def test_score_boxes_best_union_and_center(self):
        truth = [0.2, 0.2, 0.6, 0.6]
        scores = score_boxes([[0.2, 0.2, 0.6, 0.6], [0.8, 0.8, 0.9, 0.9]], truth)
        self.assertEqual((scores["best_iou"], scores["hit50"], scores["center_hit"], scores["n_boxes"]), (1.0, True, True, 2))
        self.assertLess(scores["union_iou"], 0.5)  # a stray second box drags the spanning box off
        self.assertEqual(score_boxes([], truth)["best_iou"], 0.0)

    def test_boxes_outside_the_image_are_clipped_before_scoring(self):
        scores = score_boxes([[-0.5, -0.5, 1.5, 1.5]], [0.0, 0.0, 1.0, 1.0])
        self.assertEqual(scores["best_iou"], 1.0)

    def test_convention_detection_and_conversion(self):
        self.assertEqual(detect_convention([[0.1, 0.2, 0.5, 0.6]], 576, 768), "unit")
        self.assertEqual(detect_convention([[100, 100, 900, 950]], 576, 768), "norm1000")
        self.assertEqual(detect_convention([[100, 100, 500, 700]], 576, 768), "absolute")
        self.assertEqual(detect_convention([], 576, 768), "absolute")
        self.assertEqual(to_unit_box([288, 384, 576, 768], 576, 768, "absolute"), [0.5, 0.5, 1.0, 1.0])
        self.assertEqual(to_unit_box([500, 500, 1000, 1000], 576, 768, "norm1000"), [0.5, 0.5, 1.0, 1.0])
        with self.assertRaises(ValueError):
            to_unit_box([1, 2, 3, 4], 10, 10, "pixels")

    def test_calibration_picks_the_scale_a_model_actually_uses(self):
        truth = [0.25, 0.25, 0.5, 0.5]
        # A model that answers on the 0-1000 scale although the image is 400x400 px.
        scaled = [([[250, 250, 500, 500]], 400, 400, truth)] * 3
        self.assertEqual(calibrate_convention(scaled)["best"], "norm1000")
        pixels = [([[100, 100, 200, 200]], 400, 400, truth)] * 3
        self.assertEqual(calibrate_convention(pixels)["best"], "absolute")


class TimingTests(unittest.TestCase):
    def test_inference_time_is_first_token_plus_decode(self):
        timing = stage_timing({"ttft_ms": 2000, "decode_time_ms": 3000, "generated_tokens": 30, "decode_tok_s": 10})
        self.assertEqual((timing["inference_s"], timing["ms_per_token"]), (5.0, 100.0))

    def test_decode_time_is_rebuilt_for_older_records(self):
        timing = stage_timing({"ttft_ms": 1000, "generated_tokens": 20, "decode_tok_s": 10})
        self.assertEqual(timing["inference_s"], 3.0)
        self.assertIsNone(stage_timing({})["inference_s"])


def app_result(response, **extra):
    base = {"status": "completed", "response": response, "ttft_ms": 1000, "decode_time_ms": 2000,
            "prompt_tokens": 300, "generated_tokens": 40, "decode_tok_s": 20.0, "stop_reason": "eos"}
    base.update(extra)
    return base


def record(label, **extra):
    base = {"pair_id": "I016", "variant": "tampered" if label == "tampered" else "original", "expected_label": label,
            "category": "Indoor", "source_kind": "nano_banana" if label == "tampered" else None,
            "edit": "add" if label == "tampered" else None, "prepared_size": [576, 768],
            "gt_box_norm": [0.25, 0.25, 0.5, 0.5] if label == "tampered" else None,
            "gt_quality": {"low_confidence": False} if label == "tampered" else None}
    base.update(extra)
    return base


class ScoreIterationTests(unittest.TestCase):
    def test_correct_tampered_answer_scores_label_edit_and_box(self):
        stage2 = json.dumps({"boxes": [{"object": "lamp", "bbox": [144, 192, 288, 384]}]})
        row = score_iteration(record("tampered"), {"iteration": 3, "stage1": app_result(STAGE1_OK), "stage2": app_result(stage2)})
        self.assertTrue(row["label_correct"] and row["binary_correct"] and row["edit_correct"] and row["schema_ok"])
        self.assertEqual((row["best_iou"], row["hit50"], row["box_convention"]), (1.0, True, "absolute"))
        self.assertEqual(row["total_inference_s"], 6.0)
        self.assertEqual(row["stage2_prompt_tokens"], 300)

    def test_fully_fake_on_a_tampered_image_is_wrong_for_three_class_but_right_for_binary(self):
        answer = json.dumps({"explanation": "x", "classification": "fully_fake", "tampered_objects": []})
        row = score_iteration(record("tampered"), {"stage1": app_result(answer), "stage2": app_result('{"boxes": []}')})
        self.assertFalse(row["label_correct"])
        self.assertTrue(row["binary_correct"])
        self.assertEqual((row["n_boxes"], row["best_iou"], row["hit50"]), (0, 0.0, False))

    def test_real_image_with_an_empty_stage_two_has_no_false_positive(self):
        answer = json.dumps({"explanation": "x", "classification": "real", "tampered_objects": []})
        row = score_iteration(record("real"), {"stage1": app_result(answer), "stage2": app_result('{"boxes": []}')})
        self.assertTrue(row["label_correct"] and row["binary_correct"])
        self.assertEqual((row["false_positive_boxes"], row["box_scored"], row["best_iou"]), (False, False, None))
        invented = score_iteration(record("real"), {"stage1": app_result(answer),
                                                    "stage2": app_result('{"boxes": [{"bbox": [1, 1, 50, 50]}]}')})
        self.assertTrue(invented["false_positive_boxes"])

    def test_low_confidence_masks_are_not_box_scored(self):
        weak = record("tampered", gt_quality={"low_confidence": True})
        row = score_iteration(weak, {"stage1": app_result(STAGE1_OK), "stage2": app_result('{"boxes": [{"bbox": [1, 1, 50, 50]}]}')})
        self.assertFalse(row["box_scored"])
        self.assertIsNone(row["best_iou"])

    def test_a_failed_stage_scores_as_missing_not_as_a_verdict(self):
        failed = {"status": "error", "response": "", "error": "out of memory"}
        row = score_iteration(record("tampered"), {"stage1": failed})
        self.assertEqual((row["predicted_label"], row["label_correct"], row["binary_correct"]), (None, False, False))
        self.assertEqual(row["stage2_status"], "missing")
        self.assertIsNone(row["total_inference_s"])

    def test_zero_to_one_thousand_answers_are_converted_when_auto_detected(self):
        stage2 = json.dumps({"boxes": [{"bbox": [250, 250, 500, 500]}]})
        # 576x768 image, so 500 is inside the image in pixels; only an explicit convention can read it as 0-1000.
        auto = score_iteration(record("tampered"), {"stage1": app_result(STAGE1_OK), "stage2": app_result(stage2)})
        forced = score_iteration(record("tampered"), {"stage1": app_result(STAGE1_OK), "stage2": app_result(stage2)}, "norm1000")
        self.assertEqual(auto["box_convention"], "absolute")
        self.assertEqual((forced["box_convention"], forced["best_iou"]), ("norm1000", 1.0))


class ExtractJsonTests(unittest.TestCase):
    def test_prefers_an_object_over_a_stray_list(self):
        value, mode = extract_json('Boxes [1, 2] then {"a": 1}')
        self.assertEqual((value, mode), ({"a": 1}, "embedded"))
        self.assertEqual(extract_json("no json here"), (None, "none"))


if __name__ == "__main__":
    unittest.main()
