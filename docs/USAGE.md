# Using PhoneBench

How to run model benchmarks on the phone from your PC (local mode), and how
the cloud backend is set up. For how the system is designed, see
[DESIGN.md](DESIGN.md); for the exact protocol, see [API.md](API.md).

Status (2026-10-05): local mode works end to end; the first real job ran on
the phone's NPU on 2026-10-03. The cloud backend (Supabase) is set up and
tested ([Cloud setup](#cloud-setup-supabase)), and the website works against
a local server; deploying it is in [Deploying to Vercel](#deploying-to-vercel).
Check results carefully until the part 6 validation against the 2026-09-18
baseline is done.

## How it fits together

```
 Your PC                                          The phone
 ┌────────────────────────────────┐   adb reverse   ┌──────────────────────┐
 │ phonebench submit / status     │◄──────────────►│ PhoneBench app        │
 │ phonebench serve (127.0.0.1)   │   port 8000     │ (background worker)   │
 │ results in server\data\jobs\   │                 │ runs models on NPU…   │
 └────────────────────────────────┘                 └──────────────────────┘
```

1. You submit a folder of images and settings with `phonebench submit`.
2. The server downscales each image as it arrives, splits the job into
   units (one model on one compute unit each) and queues them.
3. The PhoneBench app asks the server for work every 10 s, runs each unit,
   and reports every run as it finishes.
4. The server writes the results to `server\data\jobs\<job_id>\outputs\`.

The [website](#the-website) does the same from a browser; the commands
below work without it.

## Requirements

- Windows with PowerShell. Commands below assume the repo is at
  `C:\Users\ryang\phone-benchmark-hub`.
- Python 3.12 or newer (`py --version`).
- Android Studio, for its bundled Java (`C:\Program Files\Android\Android Studio\jbr`)
  and the Android SDK. Needed only to build the app.
- `adb` on your PATH, and the phone paired over wireless debugging (or USB).
- The phone and PC on the same network (the PC's hotspot works).

## One-time setup

### 1. The server

```powershell
cd C:\Users\ryang\phone-benchmark-hub\server
py -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
```

The tests should all pass. Run the `pip install` line again after pulling
changes that add dependencies. The first time the server starts (next
section), it creates the phone's device key in `server\data\device_key.txt`.

### 2. Build and install the app

```powershell
cd C:\Users\ryang\phone-benchmark-hub\worker
$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'
.\gradlew.bat assembleDebug
adb install -r build\outputs\apk\debug\phonebench-debug.apk
```

The first build downloads a few libraries; later builds can add `--offline`.
The server requires app version 0.2.0 or newer (`versionName` in
`worker\build.gradle`); an older app shows "Update the app".

### 3. Set up the phone

Start the server once (see [Every session](#every-session)) so the device key
exists, then:

```powershell
cd C:\Users\ryang\phone-benchmark-hub
adb shell pm grant com.rred5.phonebench android.permission.POST_NOTIFICATIONS
adb shell dumpsys deviceidle whitelist +com.rred5.phonebench
$key = (Get-Content server\data\device_key.txt).Trim()
adb shell am broadcast -f 0x20 -n com.rred5.phonebench/.SetupReceiver --es server_url http://127.0.0.1:8000 --es device_key $key
Remove-Variable key
```

The last command should print `data="server=http://127.0.0.1:8000 key=set"`.

- The setup receiver only accepts commands from adb, so other apps on the
  phone cannot change the server or key.
- Debug builds accept plain `http://` only for `127.0.0.1`/`localhost`;
  anything else must be `https://`.

Also on the phone, once: **Settings → Apps → PhoneBench → Battery →
Unrestricted**, and make sure PhoneBench is not in Samsung's **Sleeping apps**
or **Deep sleeping apps** lists (under Battery → Background usage limits; the
exact menu names vary by One UI version). Samsung otherwise stops background
apps.

## Every session

**1. Start the server** in its own PowerShell window, and leave it open:

```powershell
cd C:\Users\ryang\phone-benchmark-hub\server
.venv\Scripts\python -m phonebench serve
```

Check it from another window: `curl.exe http://127.0.0.1:8000/api/v1/health`.
While it runs, `http://127.0.0.1:8000/docs` in a browser lists every API call.

**2. Connect the phone:**

```powershell
adb devices                    # exactly one phone should be listed
adb reverse tcp:8000 tcp:8000  # repeat after every adb reconnect
```

**3. Start the worker** if its notification is not showing: open PhoneBench
and tap **Start worker**, or

```powershell
adb shell am start -n com.rred5.phonebench/.MainActivity --ez start_worker true
```

Within about 10 s, `phonebench status` should show the phone as `idle`.

## Submitting a job

From a second PowerShell window:

```powershell
cd C:\Users\ryang\phone-benchmark-hub\server
.venv\Scripts\python -m phonebench submit C:\path\to\images --name "2B vs 4B" --settings C:\path\to\settings.json
```

- The command uploads the folder (including subfolders), skips anything that
  is not an image and says why, then prints the job ID, its number of runs
  and an estimated duration.
- Without `--settings`, the job uses [config/defaults.json](../config/defaults.json):
  Qwen3-VL-2B on NPU, warm, 10 repetitions, 10 s rest, 768 px, the LPCVC
  prompt.
- The first job that uses a model makes the phone download it (2.5 GB for
  Qwen3-VL-2B, 3.7 GB for Qwen3-VL-4B).

### Images and labels

- Accepted: PNG, JPEG and WebP, up to 32 MB and 8192 px on the long edge.
  Files whose names start with `.` are skipped.
- Every image is downscaled to `max_edge` on the long edge before the phone
  sees it (aspect ratio kept, Lanczos, never upscaled, transparency flattened
  onto white). This is the same processing as
  `android\prepare_benchmark_image.py`.
- Images inside a folder named `real`, `synthetic` or `tampered` (any case)
  get that label and are scored for accuracy. Other images get responses and
  speed metrics only.

```
my-images\
  real\       L106.png  S059.png
  tampered\   I016.png
  extra.png             ← no label: not scored
```

### Settings file

A JSON file that overrides any part of the defaults. Example:

```json
{
  "run": {
    "models": ["Qwen3-VL-2B-Instruct-GGUF", "Qwen3-VL-4B-Instruct-GGUF"],
    "prompts": ["Is this image real, fully synthetic, or tampered?"],
    "backends": ["NPU", "GPU"],
    "mode": "Warm",
    "repetitions": 5,
    "rest_seconds": 10
  },
  "preprocessing": { "max_edge": 768 }
}
```

| Field | Meaning | Allowed | Default |
|---|---|---|---|
| `run.models` | Models to compare | IDs below | `["Qwen3-VL-2B-Instruct-GGUF"]` |
| `run.prompts` | Prompts to compare | 1–5, each ≤ 2,000 characters | the LPCVC prompt |
| `run.backends` | Compute units | any of `CPU`, `GPU`, `NPU` | `["NPU"]` |
| `run.mode` | `Warm`: load once, one excluded warm-up, context reset before each run. `Cold`: load and unload for every run | `Warm`, `Cold` | `Warm` |
| `run.repetitions` | Runs per image and prompt | 1–30 | 10 |
| `run.rest_seconds` | Pause between runs | 0–60 | 10 |
| `run.max_tokens` | Longest response | 1–4096 | 2048 |
| `preprocessing.max_edge` | Long edge of the image sent to the model | 256–2048 | 768 |
| `output.save_response` | Keep response text in `runs.csv` | true/false | true |
| `output.save_raw_result` | Keep each run's raw JSON | true/false | true |
| `output.save_csv` / `output.save_summary` | Write `runs.csv` / `summary.json` | true/false | true |
| `output.metrics` | Metric columns | any of `prompt_tokens`, `generated_tokens`, `reported_total_tokens`, `ttft_ms`, `prefill_tok_s`, `decode_tok_s` | all six |

The harness's old single `model` and `prompt` fields still work.
`run.images` is filled in by `submit`; `image` and `pairs` are not accepted.

Model IDs ([config/models.json](../config/models.json)):

| ID | Runs on |
|---|---|
| `Qwen3.5-0.8B-GGUF` | CPU, GPU, NPU |
| `Qwen3.5-2B-GGUF` | CPU, GPU, NPU |
| `Qwen3-VL-2B-Instruct-GGUF` | CPU, GPU, NPU |
| `Qwen3-VL-4B-Instruct-GGUF` | CPU, GPU, NPU |
| `Qwen2.5-VL-7B-Instruct` | NPU only; built for SM8750 and may not run on this phone (SM8850) |

**Job size.** Runs = models × backends × images × prompts × repetitions; the
limit is 500 per job, and at most 3 jobs can be queued or running at once. A
run takes roughly a minute, so check the estimate `submit` prints.

**Note on `max_edge`.** Qwen3-VL's vision encoder accepts up to about
2048×2048 pixels and costs more tokens for bigger images, and the phone's
context is limited. Raise `max_edge` above 768 with care; see "Notes for
later" in DESIGN.md.

## Checking progress

```powershell
.venv\Scripts\python -m phonebench status             # phone state and all jobs
.venv\Scripts\python -m phonebench status j_1234abcd  # one job in detail (JSON)
```

The phone's state is `idle`, `downloading` (a model), `running`, `paused`,
`error` or `offline` (no contact for 30 s).

Job states: `queued` → `running` → `done`, `failed` or `cancelled`. A job is
`failed` only if part of it failed 3 times in a row; individual failed runs
show up as `failed` rows in `runs.csv`.

**Cancel a job:**

```powershell
curl.exe -X POST http://127.0.0.1:8000/api/v1/jobs/j_1234abcd/cancel
```

The phone stops within about 10 s; finished runs are kept.

## Results

Each finished job's results are in `server\data\jobs\<job_id>\outputs\`, and
can also be opened in a browser while the server runs:
`http://127.0.0.1:8000/api/v1/jobs/<job_id>/outputs/runs.csv` (or
`summary.json`, `accuracy.json`, `job.json`, or `outputs.zip` for all of
them). Those links are built fresh from the database, so they also show
results so far while a job is running.

| File | Contents |
|---|---|
| `runs.csv` | One row per run, in the order they ran (see below) |
| `summary.json` | Per model, compute unit and prompt: attempted, passed, failed, and the mean and SD of each metric over passed runs |
| `accuracy.json` | Only when some images are labeled. Per model, compute unit and prompt, overall and per label: strict, lenient and real-vs-manipulated accuracy. A failed run counts as attempted and wrong |
| `job.json` | The job, its full settings, its images (name, label, size, SHA-256) and prompts |
| `runs\<run_id>.json` | Each run's raw report from the phone |

`runs.csv` columns:

| Column | Meaning |
|---|---|
| `sequence`, `run_id` | Run order and ID |
| `model`, `backend`, `mode` | What ran |
| `image`, `label`, `prompt`, `repetition` | Image path within your folder, its label, prompt number (1-based), repetition |
| `status` | `passed`, `failed`, `cancelled` or `not_run` |
| `prompt_tokens` … `decode_tok_s` | The metrics chosen in `output.metrics` |
| `load_ms` | Model load time, on the run that loaded it |
| `warm`, `runtime`, `compute_unit`, `framework`, `framework_version` | How it ran (e.g. `geniex 0.3.5`, `llama_cpp`) |
| `battery_c`, `thermal_status`, `free_memory_mb` | Phone condition after the run |
| `started_utc`, `finished_utc`, `elapsed_s` | Timing |
| `response`, `error` | The model's answer, or why the run failed |
| `expected_label` … `binary_correct` | Scoring, for labeled images only |

Scoring is the same as the old accuracy pilot (`android\run_accuracy_pilot.py`):

- **strict** (`predicted_label`, `classification_*`): only a bare
  `{"classification": "..."}` answer counts.
- **lenient** (`diagnostic_*`): reads a verdict from prose, handling
  negation such as "no signs of manipulation".
- **binary** (`binary_*`): real vs manipulated (synthetic and tampered
  together).

The figure scripts in `android\` (`make_accuracy_figures.py`,
`make_accuracy_contact_sheet.py`) still expect the old harness's folder layout
and have not been adapted to these outputs yet.

### Storage

Each original is deleted as soon as it is downscaled; the downscaled images
(in `server\data\files\`) once their job finishes. Uploads not used in a
job are deleted after 24 hours. Results stay in the database, and the files
in `server\data\jobs\<job_id>` until you delete them. Total image storage
is capped at 800 MB.

## On the phone

The PhoneBench app shows the worker's state, the current job and run, the
model and compute unit, download progress and the last error. Buttons:

| Button | Effect |
|---|---|
| **Start worker** | Starts the background worker; it also restarts after a reboot |
| **Pause** | Finishes the current run, hands the rest back to the queue, then takes no work (stays paused after a reboot) |
| **Resume** | Takes work again |
| **Stop current job** | Cancels the running job now |
| **Quit worker** | Hands back any work and stops the worker; it won't restart after a reboot |

Pause and Stop job are also on the notification. While the worker runs:

- Don't use the GenieX chat app; both would load a model at once.
- The phone can be locked with the screen off.
- The worker loads the model at the start of each unit and unloads it at the
  end, so an idle phone holds no model memory.

Phone logs: `adb logcat -s PhoneBench GenieXSdk`. Each finished unit logs a
`UNIT_FINISHED` line.

## Troubleshooting

| Problem | Fix |
|---|---|
| `adb: more than one device` | The phone is listed twice (by IP and by name). `adb disconnect <ip:port>` for one of them |
| `adb` can't connect after a while | Re-pair: Developer options → Wireless debugging → Pair device with pairing code, then `adb pair <ip:port> <code>`. Turning on **Disable adb authorization timeout** helps |
| Phone shows "Can't reach the server" | The server isn't running, or `adb reverse tcp:8000 tcp:8000` was lost when adb reconnected |
| Phone shows "Device key rejected" | The key changed. Re-run the setup broadcast with the key in `server\data\device_key.txt` |
| Phone shows "Update the app" | Rebuild and reinstall the app |
| Phone shows "Not set up" | Run the setup broadcast |
| `serve` fails: address already in use | Another server is running. Stop it, or use `serve --port 8001` together with `adb reverse tcp:8001 tcp:8001` and `server_url http://127.0.0.1:8001` |
| `Job rejected: …` | The message names the setting and the limit |
| `Skipped <file>: …` | The file isn't PNG, JPEG or WebP, is over 32 MB / 8192 px, or storage is full |
| Job stays `running` with the phone offline | After 3 minutes without contact, its work goes back in the queue and resumes when the phone returns. A run during which the phone died twice is skipped and marked failed |
| Build: "Gradle requires JVM 17" | Set `$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'` |
| Build with `--offline` can't find a library | Build once without `--offline` |

**New device key:** `.venv\Scripts\python -m phonebench device-key` replaces
it (the old one stops working), then re-run the setup broadcast.

**Testing without the phone:** `.venv\Scripts\python -m phonebench fake-worker --once`
runs queued jobs with a simulated phone that returns made-up results. Don't
mix it with real jobs you care about.

## Cloud setup (Supabase)

Cloud mode is the same server with its database and files in a Supabase
project instead of `server\data\`. It is what the website will use. This
setup was done on 2026-10-05; repeat it only for a new Supabase project.

**1. The `.env` file.** At the repo root, `C:\Users\ryang\phone-benchmark-hub\.env`.
Git ignores it; never commit or share it.

```
SUPABASE_URL=https://<project>.supabase.co
SUPABASE_SECRET_KEY=sb_secret_...
PHONEBENCH_DATABASE_URL=postgresql://postgres.<project>:<password>@aws-0-us-east-1.pooler.supabase.com:6543/postgres
```

- `SUPABASE_URL`: Supabase dashboard → Project Settings → Data API.
- `SUPABASE_SECRET_KEY`: Project Settings → API Keys → secret key. It can
  do anything in the project; if it leaks, delete it there and make a new
  one.
- `PHONEBENCH_DATABASE_URL`: the **Connect** button → Connection String →
  **Transaction pooler** (port 6543), with your database password in place
  of `[YOUR-PASSWORD]`, without the brackets. The password is shown only
  when the project is made; reset it under Project Settings → Database if
  lost. Use letters and digits, since `@ # / : %` break the URL.

**2. Tables and storage.** From `server\`:

```powershell
.venv\Scripts\python -m phonebench --env ..\.env init-db
```

This applies `supabase\migrations\`: the tables (row level security on, no
access through Supabase's public API) and the private `phonebench` bucket
(32 MB per file, images and result files only). It is safe to repeat. The
Supabase GitHub integration also applies new migration files when they reach
`main`, so a schema change is a new file in that folder.

**3. Members.** Only these emails can sign in to the website:

```powershell
.venv\Scripts\python -m phonebench --env ..\.env members list
.venv\Scripts\python -m phonebench --env ..\.env members add teammate@gmail.com
.venv\Scripts\python -m phonebench --env ..\.env members add someone@gmail.com --admin
.venv\Scripts\python -m phonebench --env ..\.env members remove teammate@gmail.com
```

Admins can cancel anyone's job; others only their own. The `members` table
can also be edited in the Supabase table editor (`is_admin` is 0 or 1).
Sign-in starts with Google accounts, so add the address people use with
Google, exactly as Google shows it (dots included).

In Supabase, under **Authentication → Sign In / Providers**, keep **Google**
on and turn **Email** off. The server already refuses anything but Google
sign-ins; this stops strangers creating accounts at all.

**4. The phone's cloud key** (when the phone moves to the cloud server,
phase 3):

```powershell
.venv\Scripts\python -m phonebench --env ..\.env device-key
```

It is saved in `server\data\device_key_cloud.txt`, separate from the local
key, and replaces any earlier cloud key.

**Testing against Postgres.** The server tests can also run against a
Postgres you can throw away (never the Supabase project): set
`PHONEBENCH_TEST_DATABASE_URL` before `pytest`; each test makes and drops
its own schema.

## The website

The website (`web/`, React) submits jobs and shows results from any
browser. In the cloud, people sign in with Google; the account must be on
the member list.

- **New job:** a name; a folder (or files) of images, where subfolders named
  `real`, `synthetic` or `tampered` label them; models; compute units; warm
  or cold; prompts (up to 5, compared side by side); repetitions, rest,
  max tokens and image size. Submitting uploads and prepares the images, 20
  at a time, then queues the job.
- **Jobs:** every job with status and progress; Cancel on your own jobs (an
  admin can cancel any). Click a job for its runs (first-token time, decode
  speed, tokens, verdict for labeled images, the response) and downloads:
  `runs.csv`, `summary.json`, `accuracy.json`, or everything as a zip.
- **Phone status** at the top: idle, running, paused, or offline with when it
  was last seen.

**Run it on your PC** against the local server (no sign-in):

```powershell
cd C:\Users\ryang\phone-benchmark-hub\server
.venv\Scripts\python -m phonebench serve          # window 1
cd C:\Users\ryang\phone-benchmark-hub\web
npm install                                         # once
npm run dev                                         # window 2: http://localhost:5173
```

### Profiles

A profile is a saved set of job settings: models, compute units, mode,
prompts, repetitions, rest, max tokens and image size. The job name and the
images are never saved. Pick one from **Profile**, above the New job card, and
it fills in the form. **Default** is built in (from `config/defaults.json`) and
cannot be removed. Once you change the form, a **Modified** tag appears with a
**Reset** link that puts the profile's settings back.

**Manage profiles** opens a window where you type a name and press **Add
profile** to save what is in the form now (the new profile is then selected),
or press **Remove** next to a profile. Names are up to 40 characters, are not
"Default", and cannot repeat another name in any capitalisation; you can keep
30 profiles. Removing the profile you are using switches the form back to
Default.

Profiles are kept in this browser (its local storage), separately for each
signed-in email. They survive reloads and restarts, and the profile you used
last is selected next time. They are not shared between browsers, computers or
people, and clearing the site's data in the browser deletes them. If the
server's limits or model list change, a saved profile is trimmed to fit when
you pick it: a model that is gone is dropped, and numbers are held to the new
ranges.

The profile code has tests that need no browser: `npm test` in `web/`.

## Deploying to Vercel

One Vercel project serves the website and the API (`api/index.py`, the
server as one Python function); `vercel.json` sets the build, the region
(`iad1`, next to the Supabase project in us-east-1), and security headers.
Every push to `main` deploys.

**1. Vercel project** (once): at vercel.com, sign in with GitHub, **Add New →
Project**, import `rred5/LPCVC_PhoneBenchApp`, framework preset **Other**.
Under **Settings → Environment Variables**, add for Production:

| Name | Value |
|---|---|
| `SUPABASE_URL` | as in `.env` |
| `SUPABASE_SECRET_KEY` | as in `.env` |
| `PHONEBENCH_DATABASE_URL` | as in `.env` |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase → Project Settings → API Keys → publishable key (public by design) |

Redeploy after changing them.

**2. Google sign-in** (once):

1. In the Google Cloud console, create a project; under **Google Auth
   Platform**, set up the consent screen (External, app name, support
   email) and publish it. Email and profile access needs no Google review.
2. **Clients → Create client → Web application**, with the authorized
   redirect URI `https://<project>.supabase.co/auth/v1/callback`.
3. In Supabase, **Authentication → Sign In / Providers**: turn on Google
   with that client ID and secret; turn Email off.
4. **Authentication → URL Configuration**: Site URL
   `https://<site>.vercel.app`; redirect URLs `https://<site>.vercel.app/**`
   and `http://localhost:5173/**`.

**3. The phone** (app 0.2.0 or newer), over adb once; after that it needs
only Wi-Fi and a charger:

```powershell
cd C:\Users\ryang\phone-benchmark-hub
adb install -r worker\build\outputs\apk\debug\phonebench-debug.apk
$key = (Get-Content server\data\device_key_cloud.txt).Trim()
adb shell am broadcast -f 0x20 -n com.rred5.phonebench/.SetupReceiver --es server_url https://<site>.vercel.app --es device_key $key
Remove-Variable key
```

The key comes from `phonebench --env ..\.env device-key` ([Cloud
setup](#cloud-setup-supabase), step 4). To go back to the local server, run
the local setup broadcast again.

**Check it:** `https://<site>.vercel.app/api/v1/health` answers
`{"ok": true, ...}`; signing in shows the phone's status; Vercel's **Logs**
tab shows the API's errors.

## Returning the phone

1. In PhoneBench, tap **Quit worker**.
2. `adb uninstall com.rred5.phonebench` — removes the app, its models, key and
   any cached images.
3. Also remove Qualcomm's demo app if installed: `adb uninstall com.geniex.demo`
   and `adb uninstall com.geniex.demo.test`.
4. On the phone: remove the PC under Wireless debugging → Paired devices, and
   turn off Developer options.
5. On the PC: `.venv\Scripts\python -m phonebench device-key`, and
   `.venv\Scripts\python -m phonebench --env ..\.env device-key` if the phone
   used the cloud, so the old keys are useless.
