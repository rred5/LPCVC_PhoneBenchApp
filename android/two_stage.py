"""Parse, score, and time responses from the two-stage tamper prompt.

Stage 1 returns a verdict and the tampered objects; stage 2, sent in the same conversation,
returns a box per object. Everything here works on saved text and the app's result records,
so a finished run can be re-scored without the phone.
"""

import json
import math
from pathlib import Path
import re

from run_accuracy_pilot import lenient_classification


PROMPTS_PATH = Path(__file__).resolve().parent / "two_stage_prompts.json"
EDITS = ("add", "remove", "replace", "occlude", "resize")
LABEL_ALIASES = {
    "real": "real", "authentic": "real", "genuine": "real", "unedited": "real", "untampered": "real",
    "tampered": "tampered", "manipulated": "tampered", "edited": "tampered", "altered": "tampered",
    "fully_fake": "fully_fake", "fully_synthetic": "fully_fake", "full_synthetic": "fully_fake",
    "synthetic": "fully_fake", "fake": "fully_fake", "ai_generated": "fully_fake",
}
BOX_KEYS = ("bbox", "bbox_2d", "box", "box_2d", "coordinates")
STAGE2_LIST_KEYS = ("boxes", "tampered_objects", "objects", "detections")
CONVENTIONS = ("absolute", "norm1000", "unit")
FENCE = re.compile(r"^```[A-Za-z]*\s*(.*?)\s*```$", re.S)
NUMBER = r"(-?\d+(?:\.\d+)?)"
BRACKET_BOX = re.compile(r"\[\s*" + r"\s*,\s*".join([NUMBER] * 4) + r"\s*\]")
PAIR_BOX = re.compile(r"\(\s*" + NUMBER + r"\s*,\s*" + NUMBER + r"\s*\)\s*,?\s*\(\s*" + NUMBER + r"\s*,\s*" + NUMBER + r"\s*\)")


def load_prompts(path: Path = PROMPTS_PATH) -> dict:
    prompts = json.loads(path.read_text(encoding="utf-8"))
    for key in ("stage1", "stage2"):
        if not isinstance(prompts.get(key), str) or not prompts[key].strip():
            raise ValueError(f"{path} needs a non-empty '{key}' prompt")
    return prompts


def render(template: str, width: int, height: int) -> str:
    """Fill the image-size placeholders. Not str.format: the prompts contain JSON braces."""
    return template.replace("{width}", str(int(width))).replace("{height}", str(int(height)))


# --- JSON and label parsing ---------------------------------------------------------------

def extract_json(text: str):
    """Return (value, mode): 'strict' if the whole reply is JSON (optionally fenced),
    'embedded' if a JSON object or list sits inside other text, else (None, 'none')."""
    stripped = (text or "").strip()
    fenced = FENCE.match(stripped)
    try:
        return json.loads(fenced.group(1) if fenced else stripped), "strict"
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for opener in ("{", "["):  # prefer objects over bare lists such as a stray "[1, 2]"
        for match in re.finditer(re.escape(opener), text or ""):
            try:
                value, _end = decoder.raw_decode(text, match.start())
            except ValueError:
                continue
            if isinstance(value, (dict, list)):
                return value, "embedded"
    return None, "none"


def normalize_label(value) -> str | None:
    if not isinstance(value, str):
        return None
    return LABEL_ALIASES.get(re.sub(r"[\s\-]+", "_", value.strip().lower()).strip("._\"'"))


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def parse_stage1(text: str) -> dict:
    """Verdict, listed objects and explanation from a stage 1 reply.

    parse_mode says how the verdict was found: strict JSON, JSON inside other text, prose via
    the lenient parser, or not at all. schema_ok is True only for a strict reply with exactly
    the requested fields.
    """
    value, mode = extract_json(text)
    result = {"label": None, "parse_mode": "none", "schema_ok": False, "explanation": None, "objects": []}
    if isinstance(value, dict):
        label = normalize_label(value.get("classification") or value.get("label") or value.get("verdict"))
        raw_objects = value.get("tampered_objects", value.get("objects", []))
        objects = []
        for entry in raw_objects if isinstance(raw_objects, list) else []:
            if isinstance(entry, dict):
                objects.append({"object": _text(entry.get("object") or entry.get("name")),
                                "edit": _text(entry.get("edit")), "location": _text(entry.get("location"))})
            elif _text(entry):
                objects.append({"object": _text(entry), "edit": None, "location": None})
        result.update(label=label, explanation=_text(value.get("explanation")), objects=objects)
        if label:
            result["parse_mode"] = mode
            result["schema_ok"] = (
                mode == "strict" and set(value) == {"explanation", "classification", "tampered_objects"}
                and isinstance(value["explanation"], str) and isinstance(raw_objects, list)
                and all(isinstance(o, dict) and {"object", "edit", "location"} <= set(o) for o in raw_objects)
            )
    if result["label"] is None:
        diagnostic = lenient_classification(text or "")
        result["label"] = {"synthetic": "fully_fake"}.get(diagnostic, diagnostic)
        result["parse_mode"] = "prose" if result["label"] else "none"
    return result


# --- boxes ----------------------------------------------------------------------------------

def _as_box(value) -> list[float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    numbers = []
    for item in value:
        if isinstance(item, bool):
            return None
        try:
            number = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        numbers.append(number)
    return numbers


def _valid(box: list[float] | None) -> bool:
    return box is not None and box[2] > box[0] and box[3] > box[1]


def parse_stage2(text: str) -> dict:
    """Boxes from a stage 2 reply, as the model wrote them (no scaling applied).

    declared_empty is True when the reply was valid JSON that listed no boxes, which is the
    right answer for an image the model called real. invalid_boxes counts entries that were
    found but unusable (wrong length, non-numeric, or zero or negative size).
    """
    value, mode = extract_json(text)
    entries = None
    if isinstance(value, dict):
        for key in STAGE2_LIST_KEYS:
            if isinstance(value.get(key), list):
                entries = value[key]
                break
    elif isinstance(value, list):
        entries = value
    boxes, invalid = [], 0
    if entries is not None:
        # A bare [x0, y0, x1, y1] is one box, not four entries.
        if _as_box(entries) is not None:
            entries = [entries]
        for entry in entries:
            label, raw = None, entry
            if isinstance(entry, dict):
                label = _text(entry.get("object") or entry.get("name") or entry.get("label"))
                raw = next((entry[key] for key in BOX_KEYS if key in entry), None)
            box = _as_box(raw)
            if _valid(box):
                boxes.append({"object": label, "box": box})
            else:
                invalid += 1
        return {"boxes": boxes, "parse_mode": mode, "declared_empty": not entries, "invalid_boxes": invalid}
    # Prose fallback: "[x0, y0, x1, y1]" or Qwen's native "(x0,y0),(x1,y1)".
    for match in PAIR_BOX.finditer(text or ""):
        box = [float(group) for group in match.groups()]
        if _valid(box):
            boxes.append({"object": None, "box": box})
        else:
            invalid += 1
    if not boxes:
        for match in BRACKET_BOX.finditer(text or ""):
            box = [float(group) for group in match.groups()]
            if _valid(box):
                boxes.append({"object": None, "box": box})
            else:
                invalid += 1
    return {"boxes": boxes, "parse_mode": "prose" if boxes else "none", "declared_empty": False, "invalid_boxes": invalid}


def detect_convention(boxes: list[list[float]], width: float, height: float) -> str:
    """Guess the coordinate scale of one reply.

    Values up to 1 are fractions; values beyond the image but within 1000 are the 0-1000
    relative scale some models use; anything else is read as pixels. A reply whose numbers
    all fit inside the image is ambiguous and is read as pixels, as the prompt asks.
    """
    peak = max((value for box in boxes for value in box), default=0.0)
    if boxes and peak <= 1.0:
        return "unit"
    if peak > max(width, height) * 1.02 and peak <= 1020:
        return "norm1000"
    return "absolute"


def to_unit_box(box: list[float], width: float, height: float, convention: str) -> list[float]:
    if convention == "unit":
        scale = (1.0, 1.0, 1.0, 1.0)
    elif convention == "norm1000":
        scale = (1000.0, 1000.0, 1000.0, 1000.0)
    elif convention == "absolute":
        scale = (width, height, width, height)
    else:
        raise ValueError(f"Unknown coordinate convention: {convention}")
    return [value / divisor for value, divisor in zip(box, scale)]


def _area(box: list[float]) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def iou(a: list[float], b: list[float]) -> float:
    inter = _area([max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])])
    union = _area(a) + _area(b) - inter
    return inter / union if union > 0 else 0.0


def score_boxes(predicted: list[list[float]], truth: list[float]) -> dict:
    """Score unit-scale predicted boxes against one ground-truth box.

    best_iou is the best single box; union_iou compares the box spanning all predictions;
    hit50 is best_iou >= 0.5; center_hit is whether any predicted box centre lies in the truth.
    """
    clipped = [[min(1.0, max(0.0, value)) for value in box] for box in predicted]
    clipped = [box for box in clipped if _area(box) > 0]
    if not clipped:
        return {"n_boxes": 0, "best_iou": 0.0, "union_iou": 0.0, "hit50": False, "center_hit": False}
    best = max(iou(box, truth) for box in clipped)
    spanning = [min(b[0] for b in clipped), min(b[1] for b in clipped),
                max(b[2] for b in clipped), max(b[3] for b in clipped)]
    centers = [((b[0] + b[2]) / 2, (b[1] + b[3]) / 2) for b in clipped]
    return {
        "n_boxes": len(clipped), "best_iou": round(best, 4), "union_iou": round(iou(spanning, truth), 4),
        "hit50": best >= 0.5,
        "center_hit": any(truth[0] <= cx <= truth[2] and truth[1] <= cy <= truth[3] for cx, cy in centers),
    }


def calibrate_convention(samples: list[tuple[list[list[float]], float, float, list[float]]]) -> dict:
    """Pick the coordinate convention that best explains a model's boxes.

    samples: (raw predicted boxes, image width, image height, ground-truth unit box).
    Returns the mean best IoU per convention and the winner, so a model that answers on the
    0-1000 scale despite the prompt is scored fairly.
    """
    means = {}
    for convention in CONVENTIONS:
        scores = []
        for boxes, width, height, truth in samples:
            unit = [to_unit_box(box, width, height, convention) for box in boxes]
            scores.append(score_boxes(unit, truth)["best_iou"])
        means[convention] = round(sum(scores) / len(scores), 4) if scores else 0.0
    best = max(CONVENTIONS, key=lambda name: means[name]) if samples else "absolute"
    return {"best": best, "mean_best_iou": means, "samples": len(samples)}


# --- timing ---------------------------------------------------------------------------------

def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def stage_timing(result: dict) -> dict:
    """Seconds spent on one stage, from the app's own profile.

    inference_s is time to first token plus decode time. Older records have no decode time,
    so it is rebuilt from generated tokens and decode speed.
    """
    ttft_ms = _number(result.get("ttft_ms"))
    tokens = _number(result.get("generated_tokens"))
    speed = _number(result.get("decode_tok_s"))
    decode_ms = _number(result.get("decode_time_ms"))
    if decode_ms is None and tokens is not None and speed:
        decode_ms = tokens / speed * 1000
    return {
        "ttft_s": None if ttft_ms is None else ttft_ms / 1000,
        "decode_s": None if decode_ms is None else decode_ms / 1000,
        "inference_s": None if ttft_ms is None or decode_ms is None else (ttft_ms + decode_ms) / 1000,
        "decode_tok_s": speed,
        "ms_per_token": decode_ms / tokens if decode_ms is not None and tokens else None,
    }


# --- one iteration --------------------------------------------------------------------------

def score_iteration(record: dict, iteration: dict, convention: str = "auto") -> dict:
    """Flatten one image's two stage results into a scored row.

    record is the manifest entry; iteration holds the app results under 'stage1' and 'stage2'
    plus optional host timing and battery readings. convention is a CONVENTIONS name, or
    'auto' to detect it per reply.
    """
    width, height = record["prepared_size"]
    expected = record["expected_label"]
    s1, s2 = iteration.get("stage1") or {}, iteration.get("stage2") or {}
    parsed1 = parse_stage1(s1.get("response", "")) if s1.get("status") == "completed" else parse_stage1("")
    parsed2 = parse_stage2(s2.get("response", "")) if s2.get("status") == "completed" else parse_stage2("")
    label = parsed1["label"]
    raw_boxes = [entry["box"] for entry in parsed2["boxes"]]
    used = convention if convention != "auto" else detect_convention(raw_boxes, width, height)
    unit_boxes = [to_unit_box(box, width, height, used) for box in raw_boxes]
    listed_edits = {o["edit"] for o in parsed1["objects"] if o["edit"]}
    row = {
        "iteration": iteration.get("iteration"), "pair_id": record["pair_id"], "variant": record["variant"],
        "expected_label": expected, "category": record.get("category"), "source_kind": record.get("source_kind"),
        "edit": record.get("edit"),
        "predicted_label": label, "label_parse": parsed1["parse_mode"], "schema_ok": parsed1["schema_ok"],
        "label_correct": label == expected,
        # Real versus manipulated: fully_fake and tampered both count as manipulated.
        "binary_correct": (label is not None) and ((label == "real") == (expected == "real")),
        "objects_listed": len(parsed1["objects"]),
        "edit_correct": (record.get("edit") in listed_edits) if expected == "tampered" else None,
        "stage1_status": s1.get("status", "missing"), "stage2_status": s2.get("status", "missing"),
        "boxes_parse": parsed2["parse_mode"], "boxes_declared_empty": parsed2["declared_empty"],
        "boxes_invalid": parsed2["invalid_boxes"], "box_convention": used if raw_boxes else None,
        "n_boxes": len(unit_boxes),
        "stage1_prompt_tokens": s1.get("prompt_tokens"), "stage1_generated_tokens": s1.get("generated_tokens"),
        "stage2_prompt_tokens": s2.get("prompt_tokens"), "stage2_generated_tokens": s2.get("generated_tokens"),
        "stage1_stop": s1.get("stop_reason"), "stage2_stop": s2.get("stop_reason"),
        "stage1_response_chars": len(s1.get("response", "") or ""), "stage2_response_chars": len(s2.get("response", "") or ""),
        "battery_c_before": iteration.get("battery_c_before"), "battery_c_after": iteration.get("battery_c_after"),
        "host_s": iteration.get("host_s"),
    }
    for name, result in (("stage1", s1), ("stage2", s2)):
        for key, value in stage_timing(result).items():
            row[f"{name}_{key}"] = value
    row["total_inference_s"] = (
        None if row["stage1_inference_s"] is None or row["stage2_inference_s"] is None
        else row["stage1_inference_s"] + row["stage2_inference_s"])
    truth = record.get("gt_box_norm")
    quality = record.get("gt_quality") or {}
    row["box_scored"] = expected == "tampered" and truth is not None and not quality.get("low_confidence", False)
    if row["box_scored"]:
        row.update(score_boxes(unit_boxes, truth))
    else:
        row.update(best_iou=None, union_iou=None, hit50=None, center_hit=None)
    row["false_positive_boxes"] = (len(unit_boxes) > 0) if expected == "real" else None
    return row
