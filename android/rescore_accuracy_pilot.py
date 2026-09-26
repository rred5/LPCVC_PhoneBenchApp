"""Rescore a completed or partial pilot without rerunning the phone.

This is useful when a model returns a recognizable bare label instead of the
requested JSON. Strict JSON accuracy remains separate from that diagnostic.
"""

import argparse
from pathlib import Path

from run_accuracy_pilot import (
    binary_fields,
    build_image_report,
    lenient_classification,
    read_json,
    strict_classification,
    write_json,
    write_report,
)


def rescore(output: Path) -> dict:
    settings = read_json(output / "run-settings.json")
    reports = []
    for image_dir in sorted(path for path in output.iterdir() if path.is_dir()):
        settings_path = image_dir / "image-settings.json"
        runs_dir = image_dir / "runs"
        if not settings_path.is_file() or not runs_dir.is_dir():
            continue
        image = read_json(settings_path)
        rows = []
        for row_path in sorted(runs_dir.glob("NPU-run-*.json")):
            if row_path.name.endswith("-result.json"):
                continue
            raw_path = runs_dir / row_path.name.replace(".json", "-result.json")
            if not raw_path.is_file():
                continue
            row = read_json(row_path)
            raw = read_json(raw_path)
            response = raw.get("response", row.get("response", ""))
            strict = strict_classification(response)
            diagnostic = lenient_classification(response)
            row.update({
                "response": response,
                "predicted_label": strict,
                "classification_valid": strict is not None,
                "classification_correct": strict == image["expected_label"] if strict is not None else False,
                **binary_fields(diagnostic, image["expected_label"]),
            })
            write_json(row_path, row)
            rows.append(row)
        if not rows:
            reports.append({"status": "error", "pair_id": image["pair_id"], "difficulty": image["difficulty"], "error": "No measured rows"})
            continue
        error = None if len(rows) == settings["repetitions"] else "Measured run session ended before all repetitions completed"
        reports.append(build_image_report(image, rows, 0.0, settings["repetitions"], error))
    return write_report(output, settings, reports)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    report = rescore(args.output.resolve(strict=True))
    print(report["summary"])


if __name__ == "__main__":
    main()
