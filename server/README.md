# PhoneBench server

The job server from [docs/API.md](../docs/API.md). It runs two ways:

- **Local mode** (the default): listens on `127.0.0.1`, has no sign-in, and
  keeps its database (SQLite), images and results in `server/data/` (ignored
  by git).
- **Cloud mode**: the same server with Supabase Postgres, Supabase storage
  and sign-in, switched on by the settings in the repo-root `.env` (see
  [Cloud mode](#cloud-mode)).

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
  `synthetic/` or `tampered/` get that label and are scored. Each image is
  downscaled to the settings' `max_edge` as it arrives.
- `--settings` overrides [config/defaults.json](../config/defaults.json);
  limits are in [config/server.json](../config/server.json).
- Results are written to `data/jobs/<job_id>/outputs/` when a job finishes:
  `runs.csv`, `summary.json`, `accuracy.json` (labeled images only),
  `job.json`, and `runs/<run_id>.json`. They are also served, always
  current, at `/api/v1/jobs/<job_id>/outputs/<file>`, plus `outputs.zip`.

## Without the phone

`python -m phonebench fake-worker --once` runs queued jobs with a simulated
phone that follows the same API.

## Tests

```powershell
.venv\Scripts\python -m pytest
```

The tests check that scoring matches `android/run_accuracy_pilot.py` and that
image preparation is byte-identical to `android/prepare_benchmark_image.py`.
`tests/test_cloud.py` covers uploads, sign-in and cloud behavior with
stand-ins for Supabase.

To run the API tests on Postgres as well, point
`PHONEBENCH_TEST_DATABASE_URL` at a Postgres you can throw away (never the
Supabase project); each test makes and drops its own schema:

```powershell
$env:PHONEBENCH_TEST_DATABASE_URL = 'postgresql://postgres@127.0.0.1:5432/postgres'
.venv\Scripts\python -m pytest
```

## Cloud mode

`--env ..\.env` loads the Supabase settings (see
[USAGE.md, Cloud setup](../docs/USAGE.md#cloud-setup-supabase)):

```powershell
.venv\Scripts\python -m phonebench --env ..\.env init-db              # apply supabase/migrations
.venv\Scripts\python -m phonebench --env ..\.env members add a@gmail.com
.venv\Scripts\python -m phonebench --env ..\.env members list
.venv\Scripts\python -m phonebench --env ..\.env device-key           # saved in data/device_key_cloud.txt
```

| File | What it does |
|---|---|
| `phonebench/store.py` | SQLite and Postgres stores; every transaction is serialized |
| `phonebench/storage.py` | A local folder or the private Supabase bucket, with signed URLs |
| `phonebench/auth.py` | Checks a Supabase sign-in token and returns the email |
| `phonebench/api.py` | All endpoints; sign-in and members are checked in `current_user` |
