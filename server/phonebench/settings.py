"""Validate a job's settings and merge them onto the server defaults.

The format is the harness's schema_version 1 settings file, extended with
`run.models`, `run.prompts` and `run.images` lists (see docs/DESIGN.md).
"""

import copy
import math

from .config import Config

METRICS = ("prompt_tokens", "generated_tokens", "reported_total_tokens", "ttft_ms", "prefill_tok_s", "decode_tok_s")
BACKENDS = ("CPU", "GPU", "NPU")
MODES = ("warm", "cold")
FRAMEWORKS = ("geniex",)
OUTPUT_FLAGS = ("save_response", "save_raw_result", "save_csv", "save_summary")

RUN_KEYS = {"framework", "models", "prompts", "images", "backends", "mode", "repetitions", "rest_seconds", "max_tokens"}
# Harness-era keys that are accepted and ignored.
IGNORED_RUN_KEYS = {"model_already_downloaded"}
IGNORED_OUTPUT_KEYS = {"save_instrumentation_log"}


class SettingsError(ValueError):
    """A job's settings are invalid; the message says which field and why."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SettingsError(message)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value) -> bool:
    return (_is_int(value) or isinstance(value, float)) and math.isfinite(value)


def _in_range(name: str, value, bounds: list, check) -> None:
    low, high = bounds
    _require(check(value) and low <= value <= high, f"{name} must be between {low} and {high}")


def _section(overrides: dict, name: str) -> dict:
    section = overrides.get(name, {})
    _require(isinstance(section, dict), f"{name} must be an object")
    return dict(section)


def _unique_strings(name: str, value) -> list[str]:
    _require(isinstance(value, list) and all(isinstance(item, str) for item in value), f"{name} must be a list of strings")
    _require(len(value) == len(set(value)), f"{name} must not repeat entries")
    return list(value)


def resolve(overrides: dict, config: Config) -> dict:
    """Return complete, normalized settings, or raise SettingsError."""
    _require(isinstance(overrides, dict), "settings must be an object")
    unknown = set(overrides) - {"schema_version", "run", "preprocessing", "output"}
    _require(not unknown, f"Unknown settings: {', '.join(sorted(unknown))}")
    _require(overrides.get("schema_version", 1) == 1, "Only settings schema_version 1 is supported")

    run_overrides = _section(overrides, "run")
    for old in ("image", "pairs"):
        _require(old not in run_overrides, f"run.{old} is replaced by run.images")
    # The harness's single model/prompt fields still work.
    for single, plural in (("model", "models"), ("prompt", "prompts")):
        if single in run_overrides:
            _require(plural not in run_overrides, f"Use run.{single} or run.{plural}, not both")
            run_overrides[plural] = [run_overrides.pop(single)]
    for key in IGNORED_RUN_KEYS:
        run_overrides.pop(key, None)
    unknown = set(run_overrides) - RUN_KEYS
    _require(not unknown, f"Unknown run settings: {', '.join(sorted(unknown))}")

    output_overrides = _section(overrides, "output")
    for key in IGNORED_OUTPUT_KEYS:
        output_overrides.pop(key, None)
    unknown = set(output_overrides) - {*OUTPUT_FLAGS, "metrics"}
    _require(not unknown, f"Unknown output settings: {', '.join(sorted(unknown))}")

    preprocessing_overrides = _section(overrides, "preprocessing")
    unknown = set(preprocessing_overrides) - {"max_edge"}
    _require(not unknown, f"Unknown preprocessing settings: {', '.join(sorted(unknown))}")

    defaults = copy.deepcopy(config.defaults)
    run = {**defaults["run"], **run_overrides}
    preprocessing = {**defaults["preprocessing"], **preprocessing_overrides}
    output = {**defaults["output"], **output_overrides}
    limits = config.limits

    _require(run["framework"] in FRAMEWORKS, f"run.framework must be one of: {', '.join(FRAMEWORKS)}")

    models = []
    for name in _unique_strings("run.models", run["models"]):
        model = next((m for m in config.models if name in (m["id"], m["display_name"])), None)
        _require(model is not None, f"Unknown model '{name}'")
        models.append(model)
    _require(models, "run.models must name at least one model")
    _require(len({m["id"] for m in models}) == len(models), "run.models must not repeat entries")

    backends = [backend.upper() for backend in _unique_strings("run.backends", run["backends"])]
    _require(backends and all(b in BACKENDS for b in backends), f"run.backends must be a subset of {', '.join(BACKENDS)}")
    _require(len(backends) == len(set(backends)), "run.backends must not repeat entries")
    for model in models:
        unsupported = [b for b in backends if b not in model["backends"]]
        _require(
            not unsupported,
            f"{model['display_name']} runs on {', '.join(model['backends'])} only; "
            "remove the others or submit it as a separate job",
        )

    prompts = run["prompts"]
    _require(isinstance(prompts, list) and prompts, "run.prompts must be a non-empty list")
    _require(len(prompts) <= limits["max_prompts"], f"run.prompts allows at most {limits['max_prompts']} prompts")
    for prompt in prompts:
        _require(isinstance(prompt, str) and prompt.strip(), "Each prompt must be non-empty text")
        _require(len(prompt) <= limits["max_prompt_chars"], f"Each prompt allows at most {limits['max_prompt_chars']} characters")

    images = _unique_strings("run.images", run["images"])
    _require(images, "run.images must list at least one image")
    _require(len(images) <= limits["max_images"], f"run.images allows at most {limits['max_images']} images")

    mode = str(run["mode"]).lower()
    _require(mode in MODES, "run.mode must be Warm or Cold")
    _in_range("run.repetitions", run["repetitions"], limits["repetitions"], _is_int)
    _in_range("run.rest_seconds", run["rest_seconds"], limits["rest_seconds"], _is_number)
    _in_range("run.max_tokens", run["max_tokens"], limits["max_tokens"], _is_int)
    _in_range("preprocessing.max_edge", preprocessing["max_edge"], limits["max_edge"], _is_int)

    for flag in OUTPUT_FLAGS:
        _require(isinstance(output[flag], bool), f"output.{flag} must be true or false")
    metrics = _unique_strings("output.metrics", output["metrics"])
    _require(set(metrics) <= set(METRICS), f"output.metrics must be a subset of: {', '.join(METRICS)}")

    total_runs = len(models) * len(backends) * len(images) * len(prompts) * run["repetitions"]
    _require(
        total_runs <= limits["max_runs_per_job"],
        f"This job has {total_runs} runs; the limit is {limits['max_runs_per_job']}",
    )

    return {
        "schema_version": 1,
        "run": {
            "framework": run["framework"],
            "models": [model["id"] for model in models],
            "prompts": list(prompts),
            "images": images,
            "backends": backends,
            "mode": mode,
            "repetitions": run["repetitions"],
            "rest_seconds": run["rest_seconds"],
            "max_tokens": run["max_tokens"],
        },
        "preprocessing": {"max_edge": preprocessing["max_edge"]},
        "output": {**{flag: output[flag] for flag in OUTPUT_FLAGS}, "metrics": metrics},
    }


def total_runs(settings: dict) -> int:
    run = settings["run"]
    return len(run["models"]) * len(run["backends"]) * len(run["images"]) * len(run["prompts"]) * run["repetitions"]
