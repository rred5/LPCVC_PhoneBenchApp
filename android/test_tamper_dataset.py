"""Offline checks for pairing, mask-to-box conversion, preparation, and run selection."""

import json
from pathlib import Path
import tempfile
import unittest

import numpy as np
from PIL import Image

from tamper_dataset import (
    build_manifest,
    discover,
    load_manifest,
    mask_to_box,
    select_records,
)


def save_mask(path: Path, size: tuple[int, int], boxes: list[tuple[int, int, int, int]], mode: str = "L") -> None:
    array = np.zeros((size[1], size[0]), dtype=np.uint8)
    for left, top, right, bottom in boxes:
        array[top:bottom, left:right] = 255
    image = Image.fromarray(array)
    if mode == "1":
        image = image.convert("1")
    elif mode == "RGBA":
        image = Image.merge("RGBA", [image, image, image, Image.new("L", size, 255)])
    image.save(path)


def make_dataset(root: Path, edits: list[tuple[str, str, str]]) -> None:
    """edits: (stem, source kind, edit). Real and tampered images are small flat PNGs."""
    for stem, kind, edit in edits:
        category = {"I": "Indoor", "L": "Landscape", "S": "Streetscene"}[stem[0]]
        real_dir = root / "Track 3 Real Images" / category
        real_dir.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (160, 120), (10, 20, 30)).save(real_dir / f"{stem}.png")
        folder = root / "Tampered Images" / ("Nano Banana" if kind == "nano" else "Qwen-Inpaint") / category
        folder.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (160, 120), (200, 20, 30)).save(folder / (
            f"{stem}_Nano2Lite_{edit}.png" if kind == "nano" else f"{stem}_qwen_{edit}.png"))
        mask = folder / (f"{stem}_target_mask_prompt.png" if kind == "nano" else f"{stem}_qwen_{edit}_mask.png")
        save_mask(mask, (80, 60), [(20, 15, 40, 30)])


class MaskToBoxTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def box(self, size, boxes, mode="L"):
        path = self.root / "mask.png"
        save_mask(path, size, boxes, mode)
        return mask_to_box(path)

    def test_single_region_is_normalized_by_mask_size(self):
        result = self.box((200, 100), [(50, 20, 100, 60)])
        self.assertEqual(result["box_norm"], [0.25, 0.2, 0.5, 0.6])
        self.assertFalse(result["quality"]["low_confidence"])

    def test_all_mask_modes_give_the_same_box(self):
        boxes = {mode: self.box((120, 90), [(30, 18, 60, 54)], mode)["box_norm"] for mode in ("L", "1", "RGBA")}
        self.assertEqual(boxes["L"], boxes["1"])
        self.assertEqual(boxes["L"], boxes["RGBA"])

    def test_box_does_not_depend_on_resolution(self):
        small = self.box((120, 90), [(30, 18, 60, 54)])["box_norm"]
        large = self.box((1200, 900), [(300, 180, 600, 540)])["box_norm"]
        for a, b in zip(small, large):
            self.assertAlmostEqual(a, b, places=2)

    def test_noise_specks_do_not_stretch_the_box(self):
        clean = self.box((200, 200), [(80, 80, 140, 140)])["box_norm"]
        noisy = self.box((200, 200), [(80, 80, 140, 140), (0, 0, 2, 2), (197, 196, 199, 198)])
        self.assertEqual(noisy["box_norm"], clean)
        self.assertEqual(noisy["quality"]["components_kept"], 1)

    def test_two_real_regions_are_joined_in_one_box(self):
        result = self.box((200, 200), [(10, 10, 50, 50), (120, 120, 180, 180)])
        self.assertEqual(result["box_norm"], [0.05, 0.05, 0.9, 0.9])
        self.assertEqual(result["quality"]["components_kept"], 2)

    def test_nearly_empty_and_empty_masks_are_low_confidence(self):
        tiny = self.box((1000, 1000), [(500, 500, 502, 502)])
        self.assertTrue(tiny["quality"]["low_confidence"])
        empty = self.box((100, 100), [])
        self.assertIsNone(empty["box_norm"])
        self.assertTrue(empty["quality"]["low_confidence"])


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "LPCVC 2027"

    def test_discover_pairs_each_source_with_its_real_image(self):
        make_dataset(self.root, [("I014", "nano", "add"), ("L015", "qwen", "remove")])
        pairs = discover(self.root)
        self.assertEqual([(p["pair_id"], p["source_kind"], p["edit"], p["category"]) for p in pairs],
                         [("I014", "nano_banana", "add", "Indoor"), ("L015", "qwen_inpaint", "remove", "Landscape")])
        self.assertEqual(pairs[0]["real"].name, "I014.png")

    def test_missing_mask_or_real_image_is_reported_not_skipped(self):
        make_dataset(self.root, [("I014", "nano", "add"), ("S020", "qwen", "replace")])
        (self.root / "Track 3 Real Images" / "Indoor" / "I014.png").unlink()
        next((self.root / "Tampered Images").rglob("S020_qwen_replace_mask.png")).unlink()
        with self.assertRaises(ValueError) as caught:
            discover(self.root)
        self.assertIn("no real image for I014_Nano2Lite_add.png", str(caught.exception))
        self.assertIn("no mask for S020_qwen_replace.png", str(caught.exception))

    def test_build_manifest_records_labels_boxes_and_neutral_filenames(self):
        make_dataset(self.root, [("I014", "nano", "add"), ("L015", "qwen", "occlude")])
        output = Path(self.directory.name) / "out"
        output.mkdir()
        manifest = build_manifest(self.root, output, 64)
        path = output / "manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        loaded = load_manifest(path)
        self.assertEqual(loaded["pairs"], ["I014", "L015"])
        by_key = {(r["pair_id"], r["variant"]): r for r in loaded["records"]}
        tampered = by_key[("I014", "tampered")]
        original = by_key[("I014", "original")]
        self.assertEqual((tampered["expected_label"], original["expected_label"]), ("tampered", "real"))
        self.assertEqual(tampered["prepared_size"], [64, 48])  # 160x120 scaled to a 64 px long edge
        self.assertEqual(tampered["gt_box_norm"], [0.25, 0.25, 0.5, 0.5])
        self.assertEqual(tampered["gt_box_px"], [16, 12, 32, 24])
        self.assertIsNone(original["gt_box_norm"])
        for record in (tampered, original):
            self.assertRegex(record["remote_filename"], r"^img-[0-9a-f]{24}\.png$")
            self.assertNotIn(record["pair_id"], record["remote_filename"])
            self.assertTrue((output / record["prepared"]).is_file())


class SelectionTests(unittest.TestCase):
    def manifest(self):
        records = []
        edits = ["add", "remove", "replace"]
        for index in range(12):
            pair = f"I{index:03d}"
            kind = "nano_banana" if index % 2 else "qwen_inpaint"
            for variant, label in (("original", "real"), ("tampered", "tampered")):
                records.append({"pair_id": pair, "variant": variant, "expected_label": label,
                                "source_kind": kind if variant == "tampered" else None,
                                "edit": edits[index % 3] if variant == "tampered" else None})
        return {"records": records}

    def test_selection_is_whole_pairs_and_repeatable(self):
        manifest = self.manifest()
        first = select_records(manifest, 8, seed=3)
        again = select_records(manifest, 8, seed=3)
        self.assertEqual([r["pair_id"] + r["variant"] for r in first], [r["pair_id"] + r["variant"] for r in again])
        self.assertEqual(len(first), 8)
        by_pair = {}
        for record in first:
            by_pair.setdefault(record["pair_id"], set()).add(record["variant"])
        self.assertTrue(all(variants == {"original", "tampered"} for variants in by_pair.values()))
        self.assertNotEqual([r["pair_id"] for r in first], [r["pair_id"] for r in select_records(manifest, 8, seed=4)])

    def test_selection_spreads_over_edits_and_sources(self):
        chosen = [r for r in select_records(self.manifest(), 6, seed=1) if r["variant"] == "tampered"]
        self.assertEqual(len({(r["source_kind"], r["edit"]) for r in chosen}), 3)

    def test_a_partial_round_takes_equally_from_each_source(self):
        manifest = self.manifest()  # six (source, edit) strata, two pairs each
        for seed in range(10):
            tampered = [r for r in select_records(manifest, 8, seed) if r["variant"] == "tampered"]
            counts = {kind: sum(1 for r in tampered if r["source_kind"] == kind) for kind in ("nano_banana", "qwen_inpaint")}
            self.assertEqual(counts, {"nano_banana": 2, "qwen_inpaint": 2}, f"seed {seed}")

    def test_odd_or_oversized_counts_are_rejected(self):
        with self.assertRaises(ValueError):
            select_records(self.manifest(), 7)
        with self.assertRaises(ValueError):
            select_records(self.manifest(), 40)


if __name__ == "__main__":
    unittest.main()
