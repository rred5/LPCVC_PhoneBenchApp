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
  -ImagePath 'C:\path\to\LPCVC\L106.png' `
  -BuildAndInstall `
  -Mode Warm `
  -Repetitions 10 `
  -Backends CPU,GPU,NPU
```

For example, an accuracy run can use a different prompt without changing code:

```powershell
.\run_benchmark.ps1 -ImagePath 'C:\path\to\LPCVC\L106.png' `
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
python .\prepare_accuracy_dataset.py 'C:\path\to\LPCVC\accuracy_dataset'
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
  --dataset 'C:\path\to\LPCVC\accuracy_dataset' `
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
  --pilot 'C:\path\to\LPCVC\pilot_2026-09-20-rescored' `
  --benchmark 'C:\path\to\LPCVC\benchmark_2026-09-18_30runs' `
  --output 'C:\path\to\LPCVC\figures'
```

## Two-stage tamper comparison

This compares runtimes (for example `llama_cpp` on the NPU against `qairt`) on the two-stage
prompt in [two_stage_prompts.json](two_stage_prompts.json) (see
[docs/two_stage_prompt.md](../docs/two_stage_prompt.md)). Each image is one conversation: stage 1
sends the image and asks for a verdict, tampered objects and an explanation; stage 2 follows up in
the same conversation, with no reset and no second image, and asks for a box per object in
absolute pixels. Stage 2 always runs. The model loads once for the whole series, so the run shows
how inference time drifts as the phone warms.

The analysis scripts need numpy, scipy and matplotlib in addition to Pillow:
`py -m pip install -r requirements-analysis.txt`.

1. **Pair the images and derive ground truth.** Point `tamper_dataset.py` at the shared folder's
   `LPCVC 2027` directory. It pairs every tampered image with the real image of the same name,
   turns each mask into a normalized box, prepares 768 px copies under neutral names, and writes
   `manifest.json`. Masks that cover almost nothing are flagged low confidence and left out of box
   scoring.

   ```powershell
   py .\tamper_dataset.py 'C:\path\to\LPCVC 2027' --output 'C:\path\to\LPCVC\datasets\tamper_comparison'
   ```

2. **Install the debug app and its test APK** (`run_benchmark.ps1 -BuildAndInstall` builds and
   installs both), with one unlocked phone connected.

3. **Run a series.** `--model` is the app catalog's display name; its entry says which runtime
   runs it. `--count` is a whole number of real/tampered pairs, balanced over source and edit
   type and interleaved so heat does not line up with one label. Add `--dry-run` to write
   `series.json` and see the plan without touching the phone. Rerun the same command to resume.

   ```powershell
   py .\run_two_stage_series.py --manifest 'C:\path\to\...\manifest.json' `
     --model 'Qwen3-VL-2B-Instruct (GGUF)' --count 50 --output 'C:\path\to\runs\llama_cpp-npu'
   ```

   Keep the phone in the same place, at the same charge, and let it cool between runs of different
   models. The runner records battery temperature and Android's thermal status on every poll.

4. **Score and plot.** Up to three runs are compared side by side. `--convention calibrate`
   (the default) picks the coordinate scale (pixels, 0-1000, or 0-1) that best explains each
   model's boxes, because some models answer on the 0-1000 scale whatever the prompt says.

   ```powershell
   py .\analyze_two_stage.py --manifest 'C:\path\to\...\manifest.json' `
     --run llama_cpp=C:\path\to\runs\llama_cpp-npu --run qairt=C:\path\to\runs\qairt `
     --output 'C:\path\to\results\two_stage_comparison'
   ```

   Output: `report.md`, `metrics.json`, one `scored_<label>.csv` per run (the numbers behind every
   figure), and these figures: `timing_summary_<label>.png` (time per image by stage, accumulated
   time, histogram and a stats table), `thermal_<label>.png` (stage 1 time, decode time per token
   and temperature against run order, with where the time levels off), `runtime_comparison.png`,
   `time_comparison.png`, `verdict_accuracy.png`, `bbox_accuracy.png` and `tokens_per_output.png`.

How the numbers are defined:

- **Inference time** of a stage is time to first token plus decode time, as the SDK reports them.
  "Board" total is the sum over stages and images; "host" total is the whole run on the PC,
  including model load, the warm-up, rests and polling.
- **Where it levels off** is the first image after which the rolling median (7 images) stays
  within 10% of the final level (median of the last quarter). It is judged on stage 1 time and on
  decode time per token, because those do comparable work for every image. Stage 2 is short when
  the model calls an image real, so the both-stage total swings with the real/tampered mix.
- **Box accuracy** is the IoU of the best predicted box against the mask-derived box, in
  coordinates normalized by each image's size. A tampered image with no box scores 0.
- **Verdict accuracy** is reported three ways: 3-class (real / tampered / fully_fake),
  real vs manipulated, and sensitivity / specificity.

Limits: the second turn depends on the SDK's incremental tokenization and on the context being big
enough for image, both prompts and both answers; a stage that errors is recorded as missing rather
than guessed. Selecting NPU records the requested compute unit, not proof that every stage used it.
