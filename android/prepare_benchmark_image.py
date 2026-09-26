"""Make a separate, aspect-preserving RGB PNG plus preprocessing metadata.

Requires Pillow: python -m pip install Pillow
"""
import argparse
import hashlib
import json
from pathlib import Path

import PIL
from PIL import Image, ImageOps


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path)
    parser.add_argument("--max-edge", type=int, default=768)
    args = parser.parse_args()
    if args.max_edge < 1:
        parser.error("--max-edge must be positive")
    source = args.image.resolve(strict=True)
    output_dir = source.parent / f"prepared_{args.max_edge}"
    output = output_dir / f"{source.stem}-{args.max_edge}.png"
    metadata = output.with_suffix(".json")
    if output.exists() or metadata.exists():
        parser.error(f"Output already exists: {output}; keep existing benchmark artifacts intact")
    original_hash = sha256(source)
    with Image.open(source) as image:
        original_size = image.size
        image = ImageOps.exif_transpose(image)
        # Flatten alpha consistently; change this background only for a new experiment.
        if "A" in image.getbands() or "transparency" in image.info:
            rgba = image.convert("RGBA")
            image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
        image = image.convert("RGB")
        # Lower max_edge for fewer image tokens; raise it for more detail.
        scale = min(1.0, args.max_edge / max(image.size))
        target = tuple(max(1, round(edge * scale)) for edge in image.size)
        if image.size != target:
            image = image.resize(target, Image.Resampling.LANCZOS)
        output_dir.mkdir(parents=True, exist_ok=True)
        image.save(output, format="PNG")
    assert sha256(source) == original_hash, "Source image changed"
    with Image.open(output) as check:
        check.load()
        assert check.format == "PNG" and check.mode == "RGB" and check.size == target
    record = {
        "source": str(source), "source_sha256": original_hash,
        "source_size": original_size, "output": str(output),
        "output_sha256": sha256(output), "output_size": target,
        "max_edge": args.max_edge, "resampling": "Lanczos",
        "crop": False, "upscale": False, "exif_orientation_applied": True,
        "alpha_background": "white", "mode": "RGB", "format": "PNG",
        "pillow_version": PIL.__version__,
    }
    metadata.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2))


if __name__ == "__main__":
    main()
