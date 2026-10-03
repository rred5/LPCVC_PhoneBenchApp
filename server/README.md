# PhoneBench server

The job server from [docs/API.md](../docs/API.md). Phase 1 runs it locally:
it listens on `127.0.0.1`, has no sign-in, and keeps its database, images and
results in `server/data/` (ignored by git).

**For the full setup with the phone, every job setting, the output files and
troubleshooting, see [docs/USAGE.md](../docs/USAGE.md).** This page is the
short version.

## Set up

From `server/`, once:

```powershell
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

## Run

```powershell
.venv\Scripts\python -m phonebench serve
```

The first start creates the phone's device key in `data/device_key.txt`.
`python -m phonebench device-key` replaces it; the old key stops working.

To let the phone reach the server over adb (USB or wireless):

```powershell
adb reverse tcp:8000 tcp:8000
```

## Submit and check jobs

```powershell
.venv\Scripts\python -m phonebench submit C:\path\to\images --settings my-settings.json --name "2B vs 4B"
.venv\Scripts\python -m phonebench status
.venv\Scripts\python -m phonebench status j_369aaef70400
curl.exe -X POST http://127.0.0.1:8000/api/v1/jobs/j_369aaef70400/cancel
```

- The folder is uploaded with its structure; images under `real/`,
  `synthetic/` or `tampered/` get that label and are scored.
- `--settings` overrides [config/defaults.json](../config/defaults.json);
  limits are in [config/server.json](../config/server.json).
- Results are written to `data/jobs/<job_id>/outputs/`: `runs.csv`,
  `summary.json`, `accuracy.json` (labeled images only), `job.json`, and
  `runs/<run_id>.json`. They are also served at
  `/api/v1/jobs/<job_id>/outputs/<file>`.

## Without the phone

`python -m phonebench fake-worker --once` runs queued jobs with a simulated
phone that follows the same API.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

The tests check that scoring matches `android/run_accuracy_pilot.py` and that
image preparation is byte-identical to `android/prepare_benchmark_image.py`.
