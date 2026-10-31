import pytest

from phonebench.config import load_config
from phonebench.settings import SettingsError, resolve, total_runs

CFG = load_config(env={})


def run(**fields) -> dict:
    return {"run": {"images": ["up_1"], **fields}}


def test_defaults_fill_everything_but_images():
    settings = resolve(run(), CFG)
    assert settings["run"]["models"] == ["Qwen3-VL-2B-Instruct-GGUF"]
    assert settings["run"]["mode"] == "warm"
    assert settings["preprocessing"]["max_edge"] == 768
    assert total_runs(settings) == 10


def test_harness_settings_still_work():
    settings = resolve(
        {
            "schema_version": 1,
            "run": {
                "model": "Qwen3-VL-2B-Instruct (GGUF)",
                "prompt": "Is this real?",
                "model_already_downloaded": True,
                "images": ["up_1"],
                "mode": "Cold",
            },
            "output": {"save_instrumentation_log": True},
        },
        CFG,
    )
    assert settings["run"]["models"] == ["Qwen3-VL-2B-Instruct-GGUF"]
    assert settings["run"]["prompts"] == ["Is this real?"]
    assert settings["run"]["mode"] == "cold"


def test_comparison_job():
    settings = resolve(
        run(
            models=["Qwen3-VL-2B-Instruct-GGUF", "Qwen3-VL-4B-Instruct-GGUF"],
            backends=["npu", "GPU"],
            prompts=["a", "b"],
            repetitions=3,
        ),
        CFG,
    )
    assert settings["run"]["backends"] == ["NPU", "GPU"]
    assert total_runs(settings) == 2 * 2 * 1 * 2 * 3


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"surprise": 1}, "Unknown settings"),
        ({"schema_version": 2, **run()}, "schema_version 1"),
        (run(repetiton=3), "Unknown run settings"),
        (run(image="L106.png"), "replaced by run.images"),
        ({"run": {"pairs": ["I016"]}}, "replaced by run.images"),
        (run(model="Qwen3-VL-2B-Instruct-GGUF", models=[]), "not both"),
        (run(models=["Nope"]), "Unknown model"),
        (run(models=["Qwen3-VL-2B-Instruct-GGUF", "Qwen3-VL-2B-Instruct (GGUF)"]), "must not repeat"),
        (run(models=["Qwen2.5-VL-7B-Instruct"], backends=["CPU", "NPU"]), "runs on NPU only"),
        (run(backends=["TPU"]), "subset of"),
        (run(prompts=[]), "non-empty list"),
        (run(prompts=["a"] * 6), "at most 5 prompts"),
        (run(prompts=["x" * 2001]), "at most 2000 characters"),
        (run(prompts=["   "]), "non-empty text"),
        ({"run": {"images": []}}, "at least one image"),
        ({"run": {"images": [f"up_{i}" for i in range(101)]}}, "at most 100 images"),
        (run(mode="hot"), "Warm or Cold"),
        (run(repetitions=0), "run.repetitions"),
        (run(repetitions=31), "run.repetitions"),
        (run(repetitions=True), "run.repetitions"),
        (run(rest_seconds=-1), "run.rest_seconds"),
        (run(max_tokens=5000), "run.max_tokens"),
        ({**run(), "preprocessing": {"max_edge": 100}}, "preprocessing.max_edge"),
        ({**run(), "output": {"metrics": ["vibes"]}}, "output.metrics"),
        ({**run(), "output": {"save_csv": "yes"}}, "output.save_csv"),
        (run(images=[f"up_{i}" for i in range(60)], repetitions=10), "the limit is 500"),
    ],
)
def test_rejects(overrides, message):
    with pytest.raises(SettingsError, match=message):
        resolve(overrides, CFG)


def test_multi_turn_prompts():
    settings = resolve(run(prompts=["single", ["first {width}x{height}", "second"], ["only one turn"]]), CFG)
    # A one-turn list is stored as plain text, like a single prompt.
    assert settings["run"]["prompts"] == ["single", ["first {width}x{height}", "second"], "only one turn"]
    # Turns share one run: they do not multiply the run count.
    assert total_runs(settings) == 3 * 10


@pytest.mark.parametrize(
    "prompt, message",
    [
        ([], "must not be empty"),
        (["a", "b", "c", "d", "e"], "at most 4 turns"),
        (["a", ""], "non-empty text"),
        (["a", 3], "non-empty text"),
        (["a", "x" * 2001], "at most 2000 characters"),
    ],
)
def test_bad_multi_turn_prompts(prompt, message):
    with pytest.raises(SettingsError, match=message):
        resolve(run(prompts=[prompt]), CFG)
