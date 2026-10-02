"""Score responses against an image's label.

The classification functions are copied unchanged from
android/run_accuracy_pilot.py. Keep the two in sync until the old harness is
removed (phase 1, part 7).
"""

import json
import re

LABELS = {"real", "synthetic", "tampered"}
BINARY_OF = {"real": "real", "synthetic": "manipulated", "tampered": "manipulated"}
CUE_LABELS = {
    "real": "real", "genuine": "real", "authentic": "real",
    "unaltered": "real", "untampered": "real", "unmodified": "real",
    "synthetic": "synthetic", "ai-generated": "synthetic", "ai generated": "synthetic",
    "computer-generated": "synthetic", "cgi": "synthetic", "fake": "synthetic",
    "fabricated": "synthetic",
    "tampered": "tampered", "tampering": "tampered", "manipulated": "tampered",
    "manipulation": "tampered", "edited": "tampered", "altered": "tampered",
    "doctored": "tampered", "photoshopped": "tampered", "composited": "tampered",
}
CUE_PATTERN = re.compile(
    r"\b(" + "|".join(re.escape(cue) for cue in sorted(CUE_LABELS, key=len, reverse=True)) + r")\b"
)
NEGATION_PATTERN = re.compile(
    r"\b(?:not|no|never|without|cannot|can't|nothing|free of|lacks|lacking|absent|n't)\b"
)
CLAUSE_SPLIT = re.compile(r"[,;:]|\bbut\b|\bhowever\b|\balthough\b|\bwhereas\b")


def strict_classification(response: str) -> str | None:
    """Accept only a bare object with the exact classification key/value."""
    def reject_duplicate_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(response.strip(), object_pairs_hook=reject_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or set(value) != {"classification"}:
        return None
    label = value["classification"]
    return label if isinstance(label, str) and label in LABELS else None


def _sentences(text: str) -> list[str]:
    return [part for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]


def _sentence_verdict(sentence: str) -> str | None:
    for match in CUE_PATTERN.finditer(sentence):
        label = CUE_LABELS[match.group(1)]
        clause = CLAUSE_SPLIT.split(sentence[: match.start()])[-1]
        if NEGATION_PATTERN.search(clause):
            # "no signs of manipulation" asserts the opposite of its cue.
            if label == "real":
                continue
            return "real"
        return label
    return None


def lenient_classification(response: str) -> str | None:
    """Label a freeform response, tolerating casing, phrasing, and negation.

    A verdict in the opening sentence wins. Otherwise the model reasoned before
    committing, so the closing verdict is its conclusion rather than a premise.
    """
    if not isinstance(response, str):
        return None
    text = response.replace("’", "'").replace("“", '"').replace("”", '"').strip()
    if not text:
        return None
    if text.strip(" \t*`\"'.!").lower() in LABELS:
        return text.strip(" \t*`\"'.!").lower()
    strict = strict_classification(response)
    if strict is not None:
        return strict
    sentences = _sentences(text.lower())
    if not sentences:
        return None
    opening = _sentence_verdict(sentences[0])
    if opening is not None:
        return opening
    for sentence in reversed(sentences[1:]):
        verdict = _sentence_verdict(sentence)
        if verdict is not None:
            return verdict
    return None


def binary_fields(diagnostic: str | None, expected: str) -> dict:
    """Score the lenient label three ways: raw, three-class, and real vs manipulated."""
    predicted_binary = BINARY_OF.get(diagnostic) if diagnostic is not None else None
    expected_binary = BINARY_OF[expected]
    return {
        "diagnostic_label": diagnostic,
        "diagnostic_valid": diagnostic is not None,
        "diagnostic_correct": diagnostic == expected if diagnostic is not None else False,
        "binary_label": predicted_binary,
        "binary_expected": expected_binary,
        "binary_correct": predicted_binary == expected_binary if predicted_binary is not None else False,
    }


def score(response: str, expected: str) -> dict:
    """All scoring fields for one response to an image labeled `expected`."""
    strict = strict_classification(response)
    return {
        "expected_label": expected,
        "predicted_label": strict,
        "classification_valid": strict is not None,
        "classification_correct": strict == expected if strict is not None else False,
        **binary_fields(lenient_classification(response), expected),
    }
