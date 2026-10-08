"""Pair real and tampered LPCVC Track 3 images and derive ground-truth boxes from masks.

Expected layout (the shared folder as downloaded)::

    <data root>/Track 3 Real Images/<Category>/<stem>.png
    <data root>/Tampered Images/Nano Banana/<folder>/<stem>_Nano2Lite_<edit>.png
    <data root>/Tampered Images/Nano Banana/<folder>/<stem>_target_mask_prompt.png
    <data root>/Tampered Images/Qwen-Inpaint/<folder>/<stem>_qwen_<edit>.png
    <data root>/Tampered Images/Qwen-Inpaint/<folder>/<stem>_qwen_<edit>_mask.png

Every tampered image is paired with the real image that has the same stem. Prepared files
use content-derived names, so the device-side filename does not reveal the label.

Requires Pillow, numpy and scipy (see requirements-analysis.txt).
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import random
import re

import numpy as np
from PIL import Image
from scipy import ndimage

from prepare_accuracy_dataset import prepare, sha256


REAL_DIR = "Track 3 Real Images"
TAMPERED_DIR = "Tampered Images"
CATEGORY_BY_PREFIX = {
    "I": "Indoor", "L": "Landscape", "N": "News", "M": "Social Media", "S": "Streetscene",
}
EDITS = ("add", "remove", "replace", "occlude", "resize")
SOURCES = (
    # source kind, image pattern, mask pattern
    ("nano_banana",
     re.compile(r"^(?P<stem>[A-Za-z]+\d+)_Nano2Lite_(?P<edit>[a-z]+)\.png$"),
     re.compile(r"^(?P<stem>[A-Za-z]+\d+)_target_mask_prompt\.png$")),
    ("qwen_inpaint",
     re.compile(r"^(?P<stem>[A-Za-z]+\d+)_qwen_(?P<edit>[a-z]+)\.png$"),
     re.compile(r"^(?P<stem>[A-Za-z]+\d+)_qwen_[a-z]+_mask\.png$")),
)
ANALYSIS_EDGE = 512  # masks are analysed at this size; boxes are stored normalized
NOISE_FRACTION = 0.02  # drop components smaller than this share of all mask pixels
LOW_AREA_FRACTION = 0.0015  # a mask covering less than this is not trusted as a box
SCHEMA_VERSION = 3


def mask_to_box(path: Path) -> dict:
    """Return the union box of a mask's main regions, normalized to 0-1, plus quality facts."""
    with Image.open(path) as opened:
        opened.load()
        mode, size = opened.mode, opened.size
        gray = np.asarray(opened.convert("L"), dtype=np.uint8)
    quality = {"mask_mode": mode, "mask_size": list(size), "area_fraction": 0.0,
               "components": 0, "components_kept": 0, "fill_ratio": 0.0, "low_confidence": True}
    peak = int(gray.max())
    if peak == 0:
        return {"box_norm": None, "quality": quality}
    binary = gray >= max(1, peak // 2)
    height, width = binary.shape
    scale = ANALYSIS_EDGE / max(height, width)
    if scale < 1:
        small = Image.fromarray((binary * 255).astype(np.uint8)).resize(
            (max(1, round(width * scale)), max(1, round(height * scale))), Image.Resampling.BOX)
        binary = np.asarray(small) >= 32
        height, width = binary.shape
    if not binary.any():
        return {"box_norm": None, "quality": quality}

    # Dilate only to decide which specks belong together; the box uses the original pixels.
    radius = max(1, round(0.01 * max(height, width)))
    labels, count = ndimage.label(ndimage.binary_dilation(binary, iterations=radius), structure=np.ones((3, 3)))
    sizes = np.bincount(labels[binary], minlength=count + 1)[1:]
    total = int(sizes.sum())
    kept = [index + 1 for index, size in enumerate(sizes) if size >= max(1, NOISE_FRACTION * total)]
    regions = ndimage.find_objects(labels)
    lefts, tops, rights, bottoms = [], [], [], []
    kept_pixels = 0
    for label in kept:
        window = regions[label - 1]
        ys, xs = np.nonzero(binary[window] & (labels[window] == label))
        ys, xs = ys + window[0].start, xs + window[1].start
        kept_pixels += len(xs)
        lefts.append(xs.min())
        tops.append(ys.min())
        rights.append(xs.max() + 1)
        bottoms.append(ys.max() + 1)
    x0, y0, x1, y1 = min(lefts), min(tops), max(rights), max(bottoms)
    box = [x0 / width, y0 / height, x1 / width, y1 / height]
    quality.update({
        "area_fraction": round(kept_pixels / binary.size, 5),
        "components": int(count), "components_kept": len(kept),
        "fill_ratio": round(kept_pixels / max(1.0, (x1 - x0) * (y1 - y0)), 3),
        "low_confidence": kept_pixels / binary.size < LOW_AREA_FRACTION,
    })
    return {"box_norm": [round(value, 5) for value in box], "quality": quality}


def discover(data_root: Path) -> list[dict]:
    """Return one entry per tampered image with its mask and real counterpart."""
    real = {
        path.stem: path
        for folder in (data_root / REAL_DIR).iterdir()
        if folder.is_dir() and folder.name != "examples"
        for path in folder.glob("*.png")
    }
    pairs, problems = [], []
    files = sorted((data_root / TAMPERED_DIR).rglob("*.png"))
    for kind, image_pattern, mask_pattern in SOURCES:
        masks = {m["stem"]: path for path in files if (m := mask_pattern.match(path.name))}
        for path in files:
            match = image_pattern.match(path.name)
            if not match:
                continue
            stem, edit = match["stem"], match["edit"]
            if edit not in EDITS:
                problems.append(f"unknown edit '{edit}': {path.name}")
            if stem not in masks:
                problems.append(f"no mask for {path.name}")
            if stem not in real:
                problems.append(f"no real image for {path.name}")
            if stem in masks and stem in real:
                pairs.append({"pair_id": stem, "category": CATEGORY_BY_PREFIX.get(stem[0], stem[0]),
                              "source_kind": kind, "edit": edit,
                              "tampered": path, "mask": masks[stem], "real": real[stem]})
    if problems:
        raise ValueError("Incomplete dataset:\n  " + "\n  ".join(problems))
    stems = Counter(pair["pair_id"] for pair in pairs)
    repeated = sorted(stem for stem, count in stems.items() if count > 1)
    if repeated:
        raise ValueError(f"Stems tampered more than once: {repeated}")
    if not pairs:
        raise ValueError(f"No tampered images found under {data_root / TAMPERED_DIR}")
    return sorted(pairs, key=lambda pair: pair["pair_id"])


def build_manifest(data_root: Path, output: Path, max_edge: int) -> dict:
    """Prepare every image under output/prepared_<max_edge>/ and describe it."""
    prepared_dir = output / f"prepared_{max_edge}"
    records = []
    for pair in discover(data_root):
        gt = mask_to_box(pair["mask"])
        for variant, label, key in (("original", "real", "real"), ("tampered", "tampered", "tampered")):
            source = pair[key]
            source_hash = sha256(source)
            remote_filename = f"img-{source_hash[:24]}.png"
            prepared_path = prepared_dir / remote_filename
            source_size, prepared_size = prepare(source, prepared_path, max_edge)
            box_norm = gt["box_norm"] if variant == "tampered" else None
            records.append({
                "pair_id": pair["pair_id"], "variant": variant, "expected_label": label,
                "category": pair["category"], "source_kind": pair["source_kind"] if variant == "tampered" else None,
                "edit": pair["edit"] if variant == "tampered" else None,
                "source": source.relative_to(data_root).as_posix(), "source_sha256": source_hash,
                "source_size": list(source_size),
                "prepared": prepared_path.relative_to(output).as_posix(),
                "prepared_sha256": sha256(prepared_path), "prepared_size": list(prepared_size),
                "remote_filename": remote_filename,
                "gt_box_norm": box_norm,
                "gt_box_px": None if box_norm is None else [
                    round(box_norm[0] * prepared_size[0]), round(box_norm[1] * prepared_size[1]),
                    round(box_norm[2] * prepared_size[0]), round(box_norm[3] * prepared_size[1])],
                "gt_mask": pair["mask"].relative_to(data_root).as_posix() if variant == "tampered" else None,
                "gt_quality": gt["quality"] if variant == "tampered" else None,
            })
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "tamper_comparison",
        "data_root": str(data_root),
        "preprocessing": {
            "max_edge": max_edge, "format": "PNG", "color_mode": "RGB", "resampling": "Lanczos",
            "crop": False, "upscale": False, "alpha_background": "white",
            "remote_filename": "img-<source_sha256_prefix>.png",
        },
        "ground_truth": {
            "box": "union of the main mask regions, [x0, y0, x1, y1] normalized to 0-1",
            "noise_fraction": NOISE_FRACTION, "low_area_fraction": LOW_AREA_FRACTION,
        },
        "pairs": sorted({record["pair_id"] for record in records}),
        "records": records,
    }


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if manifest.get("kind") != "tamper_comparison":
        raise ValueError(f"{path} is not a tamper_comparison manifest")
    manifest["root"] = str(path.parent)
    return manifest


def _round_order(strata: dict[tuple, list[str]], rng: random.Random) -> list[tuple]:
    """Order the strata for one round, alternating sources.

    A round that is cut short (the count is not a multiple of the strata) then takes about
    the same number from each source, instead of always the alphabetically first ones.
    """
    by_source: dict[str, list[tuple]] = {}
    for key in sorted(strata):
        by_source.setdefault(key[0], []).append(key)
    for keys in by_source.values():
        rng.shuffle(keys)
    queues = [by_source[source] for source in sorted(by_source)]
    order = []
    while any(queues):
        for queue in queues:
            if queue:
                order.append(queue.pop())
    return order


def select_records(manifest: dict, count: int, seed: int = 0) -> list[dict]:
    """Pick `count` images as whole real/tampered pairs, balanced over source and edit.

    The run order interleaves labels (a seeded coin flip per pair) so that heat building up
    during a run does not line up with one label.
    """
    by_pair: dict[str, dict[str, dict]] = {}
    for record in manifest["records"]:
        by_pair.setdefault(record["pair_id"], {})[record["variant"]] = record
    if count % 2 or count < 2:
        raise ValueError("count must be an even number of images (whole pairs)")
    wanted = count // 2
    if wanted > len(by_pair):
        raise ValueError(f"Only {len(by_pair)} pairs available; asked for {wanted}")
    rng = random.Random(seed)
    strata: dict[tuple, list[str]] = {}
    for pair_id, variants in sorted(by_pair.items()):
        tampered = variants["tampered"]
        strata.setdefault((tampered["source_kind"], tampered["edit"]), []).append(pair_id)
    for members in strata.values():
        rng.shuffle(members)
    chosen: list[str] = []
    while len(chosen) < wanted:
        for key in _round_order(strata, rng):
            if strata[key] and len(chosen) < wanted:
                chosen.append(strata[key].pop())
    rng.shuffle(chosen)
    ordered = []
    for pair_id in chosen:
        pair = [by_pair[pair_id]["tampered"], by_pair[pair_id]["original"]]
        if rng.random() < 0.5:
            pair.reverse()
        ordered.extend(pair)
    return ordered


def summarize(manifest: dict) -> str:
    tampered = [record for record in manifest["records"] if record["variant"] == "tampered"]
    lines = [f"{len(manifest['records'])} images in {len(manifest['pairs'])} pairs"]
    lines.append("tampered by source: " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(r["source_kind"] for r in tampered).items())))
    lines.append("tampered by edit: " + ", ".join(f"{k}={v}" for k, v in sorted(Counter(r["edit"] for r in tampered).items())))
    low = [r["pair_id"] for r in tampered if r["gt_quality"]["low_confidence"]]
    lines.append(f"low-confidence masks (left out of box scoring): {low or 'none'}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("data_root", type=Path, help="The shared folder's 'LPCVC 2027' directory")
    parser.add_argument("--output", type=Path, required=True, help="Where to write manifest.json and prepared_<max-edge>/")
    parser.add_argument("--max-edge", type=int, default=768)
    args = parser.parse_args()
    if args.max_edge < 1:
        parser.error("--max-edge must be positive")
    data_root = args.data_root.resolve(strict=True)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(data_root, output, args.max_edge)
    path = output / "manifest.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    print(summarize(manifest))
    print(f"Manifest: {path}")


if __name__ == "__main__":
    main()
