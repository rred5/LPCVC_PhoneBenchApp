# LPCVC performance benchmark

This runs one prepared image and prompt through `Qwen3-VL-2B-Instruct (GGUF)`
on CPU, GPU, and NPU. It measures the SDK-reported prompt tokens, generated
tokens, time to first token (TTFT), prefill speed, and decode speed. It does not
evaluate classification accuracy or validate a generated mask.

## Run it

Edit [benchmark_defaults.json](benchmark_defaults.json) to set the normal model,
prompt, run mode, backends, repetitions, image size, and saved artifacts. The
launcher uses it automatically; a supplied command-line option overrides only
that setting.

Use `output.metrics` to choose the numeric columns in `runs.csv` and
`summary.json`. Set any `save_*` option to `false` to omit that artifact. Keep
`save_response` and `save_raw_result` enabled for accuracy experiments.

From PowerShell in this repository, with one unlocked Android phone connected:

```powershell
.\run_benchmark.ps1 `
  -ImagePath 'C:\Users\ryang\Downloads\LPCVC\L106.png' `
  -BuildAndInstall `
  -Mode Warm `
  -Repetitions 10 `
  -Backends CPU,GPU,NPU
```

For example, an accuracy run can use a different prompt without changing code:

```powershell
.\run_benchmark.ps1 -ImagePath 'C:\Users\ryang\Downloads\LPCVC\L106.png' `
  -SettingsFile '.\benchmark_defaults.json' `
  -Mode Warm `
  -Repetitions 1
```

`-Mode Cold` loads and unloads the model for every case. `-Mode Warm` loads it
once per backend, runs one excluded warm-up, then resets only the VLM context
before each measured case. Use different output folders for the two modes.

Cold cases stay in the same app session. Each case resets context, loads the
selected backend, runs, then unloads before the next backend reloads. Set
`run.model_already_downloaded` to `true` after the model is cached to skip the
download action. Use `-ModelAlreadyDownloaded $true` for a one-off override.

The command prepares a separate 768-pixel-longest-edge RGB PNG, verifies its
SHA-256 during transfer, and resumes an interrupted run when the output folder
has the same settings. Preparation uses Pillow; the PC runner otherwise uses
Python's standard library, ADB, the debug APK, and its Android test APK.

## What is recorded

Each completed inference is first written by the app as an atomic JSON record in
its debug-private `files/benchmark/results/` directory. The PC runner copies
that record to the output folder according to `output` in the settings file.
The record includes the exact prompt as well as the response and metrics.

- `CPU-run-01.json` — normalized row used for summaries.
- `CPU-run-01-result.json` — app response and raw SDK metrics.
- `CPU-instrumentation.txt` or `CPU-run-01-instrumentation.txt` — UI test log.
- `runs.csv` and `summary.json` — all attempts and per-backend means.

Failures remain in the output and are excluded from successful-run means.

## Limits to keep in mind

The Android test still uses the app UI to select, load, and send to the model;
those UI resource IDs and the model display name come from the GenieX sample.
The response and profiling metrics no longer depend on RecyclerView scraping or
formatted profile text. Debug-only broadcasts attach the private image, start a
recorded run, and reset context. They are excluded from release builds.

The model's sampler currently uses SDK defaults, so response length can vary.
Token totals are the SDK's text metrics; image embeddings are not independently
counted. Selecting NPU records the requested compute option, not proof that
every vision and language stage used NPU.

## Accuracy dataset

Create two folders under a dataset root: `raw/` for real images and `tampered/`
for manipulated images. Then prepare them and write ground-truth labels:

```powershell
python .\prepare_accuracy_dataset.py 'C:\Users\ryang\Downloads\LPCVC\accuracy_dataset'
```

The command writes prepared RGB PNGs and `accuracy_manifest.json`. Each record
keeps source and prepared hashes, dimensions, and its expected image label.
`expected_mask` is intentionally empty until a mask file is available for a
tampered image.

## Small accuracy pilot

For a quick NPU pilot, keep originals in `raw/`, realistic edits in
`tampered/Realisti/`, and obvious edits in `tampered/Unrealistic/`. Matching
stems pair the files: `I016.png`, `I016_RT.png`, and `I016_UT.png`.

The pilot prepares the images automatically, uses neutral hash-based filenames
on the phone, runs one excluded warm-up plus three measured NPU repetitions per
image, and checkpoints each pair/level separately:

```powershell
python .\run_accuracy_pilot.py `
  --dataset 'C:\Users\ryang\Downloads\LPCVC\accuracy_dataset' `
  --pairs I016,L080,L104 `
  --repetitions 3
```

Defaults are in `accuracy_pilot_defaults.json`; command-line values override
them. `accuracy_report.md` and `accuracy_report.json` contain per-level
accuracy, invalid-response counts, per-image agreement, and timing means and
standard deviations. Each image directory retains raw responses, SDK JSON,
instrumentation output, and its checkpoint. A diagnostic contact sheet can be
made with `make_accuracy_contact_sheet.py`.

## How responses are scored

Each response is scored three ways, because the original prompt invites prose
rather than a fixed schema:

- **Strict JSON** accepts only a bare `{"classification": "..."}` object. This
  measures output-format compliance and is not a correctness score.
- **Lenient 3-class** reads a verdict out of prose. It is case-insensitive,
  accepts a bare label, and is negation-aware, so "no signs of manipulation"
  scores as `real` rather than as a tampering claim. A verdict in the opening
  sentence wins; otherwise the model reasoned before committing, so the closing
  verdict is taken as its conclusion.
- **Real vs manipulated** collapses `synthetic` and `tampered` into one positive
  class. A locally edited image can defensibly be called either, so this is
  usually the more meaningful headline.

Agreement across repetitions is measured over parsed runs only, and is blank
when nothing parsed — it cannot read as 100% merely because every run failed to
parse.

Use `rescore_accuracy_pilot.py <output_dir>` to re-score a completed run without
touching the phone, and `make_accuracy_figures.py` to render presentation
figures and a `metrics.json` from a scored pilot:

```powershell
py .\make_accuracy_figures.py `
  --pilot 'C:\Users\ryang\Downloads\LPCVC\pilot_2026-09-20-rescored' `
  --benchmark 'C:\Users\ryang\Downloads\LPCVC\benchmark_2026-09-18_30runs' `
  --output 'C:\Users\ryang\Downloads\LPCVC\figures'
```
