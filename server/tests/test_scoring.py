import sys

import pytest

from phonebench import scoring
from phonebench.config import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "android"))
import run_accuracy_pilot as harness  # noqa: E402

RESPONSES = [
    '{"classification": "tampered"}',
    '{"classification": "tampered", "classification": "real"}',
    "Real.",
    "The image shows no signs of manipulation.",
    "This photo appears to be AI-generated.",
    "Looking closely at the shadows. However, I believe it has been doctored.",
    "I cannot tell.",
    "",
    "It isn't fake; it's an authentic photograph.",
]


@pytest.mark.parametrize("response", RESPONSES)
def test_matches_the_harness(response):
    assert scoring.strict_classification(response) == harness.strict_classification(response)
    assert scoring.lenient_classification(response) == harness.lenient_classification(response)
    for label in ("real", "synthetic", "tampered"):
        assert scoring.binary_fields(scoring.lenient_classification(response), label) == harness.binary_fields(
            harness.lenient_classification(response), label
        )


def test_score_fields():
    result = scoring.score("The image shows no signs of manipulation.", "real")
    assert result["predicted_label"] is None and not result["classification_valid"]
    assert result["diagnostic_label"] == "real" and result["diagnostic_correct"]
    assert result["binary_correct"]
