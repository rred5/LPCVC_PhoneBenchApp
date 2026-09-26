"""Make a small visual check of selected prepared accuracy images."""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps


DIFFICULTY_ORDER = ("original", "realistic", "obvious")


def selected_pairs(value: str, available: list[str]) -> list[str]:
    if value.isdigit():
        count = int(value)
        if count < 1:
            raise ValueError("pair count must be positive")
        return available[:count]
    requested = [part.strip() for part in value.split(",") if part.strip()]
    missing = [pair for pair in requested if pair not in available]
    if missing:
        raise ValueError(f"Unknown pair(s): {', '.join(missing)}")
    return requested


def make_contact_sheet(manifest_path: Path, output_path: Path, pairs_value: str, tile_size: tuple[int, int]) -> Path:
    root = manifest_path.parent
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    records = manifest["records"]
    available = list(manifest.get("pairs") or sorted({record["pair_id"] for record in records}))
    selected = selected_pairs(pairs_value, available)
    by_key = {(record["pair_id"], record["difficulty"]): record for record in records}
    margin = 16
    label_height = 28
    width = margin + len(DIFFICULTY_ORDER) * (tile_size[0] + margin)
    height = margin + len(selected) * (tile_size[1] + label_height + margin)
    sheet = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(sheet)
    for row, pair_id in enumerate(selected):
        for column, difficulty in enumerate(DIFFICULTY_ORDER):
            record = by_key[(pair_id, difficulty)]
            image_path = root / record["prepared"]
            with Image.open(image_path) as image:
                image.load()
                tile = ImageOps.contain(image.convert("RGB"), tile_size)
            x = margin + column * (tile_size[0] + margin)
            y = margin + row * (tile_size[1] + label_height + margin)
            tile_x = x + (tile_size[0] - tile.width) // 2
            tile_y = y + (tile_size[1] - tile.height) // 2
            sheet.paste(tile, (tile_x, tile_y))
            draw.rectangle((x, y, x + tile_size[0], y + tile_size[1]), outline="#888888")
            draw.text((x, y + tile_size[1] + 5), f"{pair_id} / {difficulty}", fill="black")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output_path, format="PNG")
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--pairs", default="3", help="Comma-separated IDs or a count from manifest order")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--tile-width", type=int, default=320)
    parser.add_argument("--tile-height", type=int, default=240)
    args = parser.parse_args()
    if args.tile_width < 1 or args.tile_height < 1:
        parser.error("tile dimensions must be positive")
    manifest = args.manifest.resolve(strict=True)
    output = args.output or manifest.parent / "accuracy_contact_sheet.png"
    try:
        result = make_contact_sheet(manifest, output.resolve(), args.pairs, (args.tile_width, args.tile_height))
    except (KeyError, ValueError, OSError) as error:
        parser.error(str(error))
    print(result)


if __name__ == "__main__":
    main()
