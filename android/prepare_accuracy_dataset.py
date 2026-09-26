"""Prepare paired accuracy images and write a reproducible manifest.

Expected layout::

    dataset/raw/<pair>.png
    dataset/tampered/Realisti/<pair>_RT.png
    dataset/tampered/Unrealistic/<pair>_UT.png

Prepared files use content-derived names so the device-side filename does not
reveal whether an image is real, realistic, or obvious.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

from PIL import Image, ImageOps


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
VARIANTS = (
    ("original", "real", Path("raw"), ""),
    ("realistic", "tampered", Path("tampered") / "Realisti", "_RT"),
    ("obvious", "tampered", Path("tampered") / "Unrealistic", "_UT"),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        image.load()
        return image.size


def prepare(source: Path, destination: Path, max_edge: int) -> tuple[tuple[int, int], tuple[int, int]]:
    with Image.open(source) as opened:
        opened.load()  # Force full decode; malformed/truncated PNGs fail here.
        source_size = opened.size
        image = ImageOps.exif_transpose(opened)
        if "A" in image.getbands() or "transparency" in image.info:
            rgba = image.convert("RGBA")
            image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
        image = image.convert("RGB")
        scale = min(1.0, max_edge / max(image.size))
        target_size = tuple(max(1, round(edge * scale)) for edge in image.size)
        if image.size != target_size:
            image = image.resize(target_size, Image.Resampling.LANCZOS)

        destination.parent.mkdir(parents=True, exist_ok=True)
        # Always encode a candidate so a reused hash filename is checked by
        # bytes, not only dimensions. This catches stale or corrupt output.
        temporary = destination.with_suffix(destination.suffix + f".tmp-{os.getpid()}")
        image.save(temporary, format="PNG")
        if destination.exists():
            if sha256(temporary) != sha256(destination):
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"Prepared collision or stale file at {destination}")
            temporary.unlink(missing_ok=True)
        else:
            temporary.replace(destination)
    return source_size, target_size


def find_pair_files(root: Path) -> dict[str, dict[str, Path]]:
    originals = {
        path.stem.casefold(): path
        for path in (root / "raw").rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    }
    if not originals:
        raise ValueError("No supported images found under raw/")

    pairs: dict[str, dict[str, Path]] = {}
    for pair_key, original in originals.items():
        pair_id = original.stem
        entries = {"original": original}
        for difficulty, _label, directory, suffix in VARIANTS[1:]:
            candidate = next(
                (
                    path
                    for path in (root / directory).rglob("*")
                    if path.is_file()
                    and path.suffix.lower() in IMAGE_SUFFIXES
                    and path.stem.casefold() == f"{pair_key}{suffix}".casefold()
                ),
                None,
            )
            if candidate is None:
                raise ValueError(f"Missing {difficulty} variant for pair {pair_id}")
            entries[difficulty] = candidate
        pairs[pair_id] = entries
    return pairs


def build_manifest(root: Path, output: Path, max_edge: int) -> dict:
    pairs = find_pair_files(root)
    records = []
    seen_remote: dict[str, str] = {}
    for pair_id in sorted(pairs, key=str.casefold):
        for difficulty, expected_label, _directory, _suffix in VARIANTS:
            source = pairs[pair_id][difficulty]
            source_hash = sha256(source)
            remote_filename = f"img-{source_hash[:24]}.png"
            previous = seen_remote.get(remote_filename)
            if previous and previous != source_hash:
                raise RuntimeError(f"Remote filename collision for {source}")
            seen_remote[remote_filename] = source_hash
            prepared_path = output / remote_filename
            source_size, prepared_size = prepare(source, prepared_path, max_edge)
            records.append(
                {
                    "pair_id": pair_id,
                    "difficulty": difficulty,
                    "expected_label": expected_label,
                    "expected_mask": None,
                    "source": source.relative_to(root).as_posix(),
                    "source_sha256": source_hash,
                    "source_size": list(source_size),
                    "prepared": prepared_path.relative_to(root).as_posix(),
                    "prepared_sha256": sha256(prepared_path),
                    "prepared_size": list(prepared_size),
                    "remote_filename": remote_filename,
                }
            )
    return {
        "schema_version": 2,
        "preprocessing": {
            "max_edge": max_edge,
            "format": "PNG",
            "color_mode": "RGB",
            "resampling": "Lanczos",
            "crop": False,
            "upscale": False,
            "alpha_background": "white",
            "remote_filename": "img-<source_sha256_prefix>.png",
        },
        "pairs": sorted(pairs, key=str.casefold),
        "records": records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", type=Path, help="Folder containing raw/ and tampered/")
    parser.add_argument("--max-edge", type=int, default=768)
    parser.add_argument("--output", type=Path, help="Defaults to dataset/prepared_<max-edge>")
    args = parser.parse_args()
    if args.max_edge < 1:
        parser.error("--max-edge must be positive")
    root = args.dataset.resolve(strict=True)
    output = (args.output or root / f"prepared_{args.max_edge}").resolve()
    manifest = build_manifest(root, output, args.max_edge)
    manifest_path = root / "accuracy_manifest.json"
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    print(f"Prepared {len(manifest['records'])} images across {len(manifest['pairs'])} pairs")
    print(f"Manifest: {manifest_path}")


if __name__ == "__main__":
    main()
