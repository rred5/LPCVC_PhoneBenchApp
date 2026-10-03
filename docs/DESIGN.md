# LPCVC PhoneBench — design

Status: draft for review, 2026-10-01. Phase 1, part 1 (repo import) is done;
nothing below it is built yet.

## Goal

Let teammates run vision-language model baselines on our Snapdragon phone from
a browser, with no PC or adb connection in the loop. Results match what the
current adb harness produces, so existing analysis keeps working.

Constraints:

- The phone is rented. The live worker only exists while we have it; after
  that the site runs in demo mode (see [Demo mode](#demo-mode)).
- The phone sits on dorm Wi-Fi, which is a public network, out of sight.
- The phone side stays minimal. Anything that can run on the server does.
- This app baselines models through the GenieX SDK. A later framework may load
  models differently, so the model-running code sits behind one small seam
  (see [Inference seam](#inference-seam)).

## Architecture

```
 Website (React) ──HTTPS──►  Server (FastAPI)  ◄──HTTPS── Phone (PhoneBench app)
        │                          │                (outgoing calls only)
        │  image uploads           ▼
        └──────────────►  Supabase: sign-in, database, file storage
```

How one request flows:

1. A teammate signs in and uploads a folder of images, then submits a job.
2. The server checks the user is on the approved list, checks and re-saves the
   images, merges the settings with the defaults, and queues the work.
3. The phone asks the server for work about every 10 s while idle.
4. The server hands it the next piece of work and links to the prepared images.
5. The phone runs the model on the requested CPU, GPU or NPU.
6. The phone sends back the response and metrics for each run as it finishes.
7. The website shows progress and results as they arrive.

The phone never accepts incoming connections, and it never holds database
credentials — only a device key for the server's phone endpoints.

### Why polling

Websockets and long polling were considered and dropped. A run takes over a
minute, so a few seconds of pickup delay does not matter; plain polling works
the same on localhost and on Vercel, and each poll doubles as the phone's
"I'm alive" signal. The phone polls again immediately after finishing a run.

## Who does what

| Phone | Server |
|---|---|
| Ask for work; send status (idle, running, paused, temperature, free memory, downloaded models) | Sign-in with an email allowlist, enforced on the server |
| Download the prepared images | Check uploads; re-save every image as a clean PNG |
| Load, warm up, reset, run and unload the model | Merge user settings with the defaults, within limits |
| Report each run's raw response and metrics | Split jobs into work, queue it, retry and reassign stalled work |
| Show status; Pause, Stop and Resume | Score responses; build stats, `runs.csv`, `summary.json`, charts |
| Re-check each job's settings before running (defense in depth) | Keep job history; admin settings |

## Stack

| Part | Choice | Why |
|---|---|---|
| Server | Python, FastAPI | Reuses the harness's settings loading, image preparation, scoring, stats and reports, with their 27 tests |
| Website | React | Pages and live charts; stats arrive from the server as JSON |
| Data | Supabase (Postgres, Auth, Storage) | Sign-in, queue and files without writing that infrastructure |
| Hosting | Vercel, one project for site and API | Free tier; runs Python |
| Phone | PhoneBench, a separate Android app (Kotlin) using the GenieX SDK | Our own code only; installs and uninstalls independently |
| Local development | Server on the PC; the phone reaches it through `adb reverse` | Works over wireless adb, even where the dorm network blocks device-to-device traffic |

The matplotlib figure scripts stay for presentation figures; the server does
not render images.

## Users and access

- **Sign-in:** Google or Microsoft accounts, checked on the server against an
  approved email list.
- **Visibility:** everyone signed in sees all jobs and results. Users cancel
  their own jobs; the admin can cancel any.
- **Admin (me):** limits, the email list and the device key live in database
  tables. For now I change them in Supabase's table editor; no admin page is
  built yet. Users carry an `is_admin` flag, so an admin page (queue pause,
  limits, allowlist, key revocation, export) can be added later without
  schema changes.

## Job format

A job is models × compute units × images × prompts × repetitions. Benchmark,
accuracy and model-comparison runs are the same kind of job; the output
settings decide what comes back.

The format is the existing settings file (`android/benchmark_defaults.json`,
`schema_version` 1) with these changes: `run.images` replaces `image` and
`run.pairs`; `run.models` and `run.prompts` are lists (the old single `model`
and `prompt` fields still work); and an optional `run.framework` (default
`geniex`) names what runs the model.

```json
{
  "schema_version": 1,
  "run": {
    "models": ["Qwen3-VL-2B-Instruct-GGUF", "Qwen3-VL-4B-Instruct-GGUF"],
    "prompts": ["Can you identify if this image is real, full synthetic, or tampered? ..."],
    "images": ["L106", "I016"],
    "backends": ["NPU", "GPU"],
    "mode": "Warm",
    "repetitions": 10,
    "rest_seconds": 10
  },
  "preprocessing": { "max_edge": 768 },
  "output": {
    "save_response": true,
    "metrics": ["prompt_tokens", "generated_tokens", "ttft_ms", "prefill_tok_s", "decode_tok_s"]
  }
}
```

The server keeps a default file; a user's file overrides fields within these
limits:

| Setting | Limit |
|---|---|
| Models | any vision model in the app's catalog (below) |
| Images per job | up to 100 |
| Prompts per job | up to 5, each up to 2,000 characters |
| Repetitions | 1–30 |
| Rest between runs | 0–60 s |
| Image size sent to the model (`max_edge`) | 256–2048 px on the long edge, default 768 |
| Uploaded image file | up to 32 MB and 8192 px on the long edge (raw LPCVC images are 12–23 MB, up to 5712 px); always downscaled to `max_edge` before the phone sees it |
| Total runs per job | admin setting, default 500; the site shows an estimated duration before submitting |
| Queued jobs per person | admin setting, default 3 |

Vision models in the catalog:

| Model | Runs on | Size |
|---|---|---|
| Qwen3.5-0.8B | CPU, GPU, NPU | — |
| Qwen3.5-2B | CPU, GPU, NPU | — |
| Qwen3-VL-2B-Instruct | CPU, GPU, NPU | 2.5 GB |
| Qwen3-VL-4B-Instruct | CPU, GPU, NPU | 3.7 GB |
| Qwen2.5-VL-7B-Instruct | NPU only (QAIRT bundle) | — |

The Qwen2.5-VL-7B bundle in the catalog is built for SM8750; our phone is an
SM8850, so it may not run (see [To verify](#to-verify)).

The exact unit of work the phone pulls, and the phone's API, are specified in
`docs/API.md` (phase 1, part 2). The proposal: one unit is one compute unit of
one job, so warm mode can load once and do one warm-up, and the phone reports
each run as it finishes.

## Images and uploads

- **Folder upload.** Users pick a folder or drag one onto the page; the
  browser keeps each file's path within the folder.
- **Labels from folder names.** Images under subfolders named `real`,
  `synthetic` or `tampered` carry that label, and only labeled images get
  accuracy scores. Unlabeled images get responses and speed metrics.
- **Shared library.** Labeled images can also be kept on the server and
  picked for a job.
- **Upload path.** The browser uploads each file straight to Supabase storage
  with a progress bar; the server then checks it and makes the prepared PNG
  at the job's `max_edge`. Originals are deleted once prepared.
- **Storage lifecycle.** Everything stays small enough for the free tier:
  - Job images live on the server only while the job is queued or running,
    and are deleted when it finishes. Results (text and metrics) are kept.
  - The phone downloads the images for the unit it is about to run and
    deletes them when that unit ends.
  - The shared library of labeled images is the only image set kept long
    term.
  - The server refuses uploads that would push total storage past a budget
    (admin setting, default 800 MB).
  - Considered and dropped: sending queued jobs' images to the phone ahead of
    time. It moves queue bookkeeping onto the phone, and active-job storage
    on the server is small.
- **Masks.** The prompt asks the model to mask tampered regions; masks are out
  of scope. Only the text response is stored.
- **Later:** zip uploads (with caps on unpacked size, file count and
  compression ratio, and no nested zips) and Google Drive or OneDrive folder
  links (needs extra account permissions; Google reviews Drive read access for
  public apps).

## Phone app

PhoneBench is a separate app with its own app ID (`com.rred5.phonebench`). It
uses the GenieX SDK directly and downloads each model the first time a job
needs it (the phone has about 180 GB free). Qualcomm's demo chat app stays
installed for the old harness until validation passes, then can be
uninstalled.

- **Background service.** A foreground service owns the model. It runs with
  the screen off and the phone locked, and restarts after a reboot. It loads
  the model at the start of each unit and unloads it at the end, matching the
  harness, so an idle phone holds no model memory.
- **Status screen.** Opening the app shows the service's live state: idle,
  running, paused or can't reach the server; the current job (who, which
  compute unit, run k of n, elapsed time); recent results; temperature; the
  loaded model. Closing the app does not affect running work.
- **Controls.** Pause finishes the current run and then stops taking work.
  Stop cancels the current job, which the server marks cancelled. Resume
  starts again. Pause and Stop are also notification buttons. A pause stays in
  effect after a reboot, and the website shows the phone as paused.
- **Models.** Downloaded on first use, while the status shows
  "downloading"; listed and deletable on the status screen.
- **Staying healthy.** Between jobs, the worker reloads the model, or restarts
  its own process, when free memory drops below a threshold or after a set
  number of runs.
- **Stay-awake mode (fallback).** If Android or Samsung stops the service with
  the screen off, a toggle keeps the screen on at minimum brightness with a
  black status screen whose text shifts position every minute. Samsung
  screens are OLED, so static bright content can burn in; the screen also
  adds heat, which is why screen-off is tried first.
- **Clean removal.** Uninstalling removes everything: the models, the device
  key, cached images and pending results. The app writes nothing outside its
  private storage.

One-time setup:

1. Install the APK over adb.
2. Set the server address and device key over adb.
3. Download the model in the app.
4. Allow notifications; set the app's battery usage to Unrestricted and keep
   it out of Samsung's "Sleeping apps" and "Deep sleeping apps" lists.
5. Connect to Wi-Fi, keep the phone on a charger, and consider delaying
   automatic system updates.

After setup, adb is not needed. When returning the phone: uninstall the apps,
remove the PC from Wireless debugging, turn off Developer options, and revoke
the device key on the server.

### Running alongside the chat app

Each app loads its own copy of the model; they cannot share memory. The phone
has about 11.4 GB of RAM, and two copies of Qwen3-VL-2B need roughly 5–7 GB.
Expected effects (untested): both run but slower, which silently skews
timings; or Android kills the worker when memory runs out; or the second load
fails. The worker cannot see other apps, so:

- every run records free memory and the thermal state, so overlapping runs
  show up in the data;
- a failed load fails the run and it is retried later; a killed worker's work
  is requeued after the timeout;
- the chat app is uninstalled from the benchmark phone after validation;
- part 6 tests running both at once on purpose and records what happens.

### Inference seam

The GenieX code sits behind one small interface: load a model on a compute
unit, run an image and prompt, reset, unload. Polling, the job protocol, the
status screen and the server do not depend on what runs the model, so a later
framework is a second implementation, not a rewrite. Every result records the
framework and its version (for example `geniex 0.3.5 / llama_cpp`).

## Benchmark validity

The new runner must reproduce the current harness's behavior:

- **Warm:** load once per compute unit, one excluded warm-up run, context reset
  before every measured run, `rest_seconds` between runs.
- **Cold:** load, run and unload for every run, in the same app session.
- **Images:** prepared on the server as an RGB PNG with the longest edge at
  `max_edge` (768 by default), the same as `prepare_benchmark_image.py`.
- **Metrics:** the same fields as today, plus battery temperature, thermal
  state, free memory, load time, whether the run was warm, and the framework.
- **Outputs:** the same files as today (`runs.csv`, `summary.json`, per-run
  JSON, accuracy reports), so the analysis and figure scripts keep working.

Validation baseline (image L106, 10 runs per cell, 2026-09-18):

| Mode | Unit | First token | Decode tok/s |
|---|---|---|---|
| Warm | CPU | 29.2 s | 27.6 |
| Warm | GPU | 14.6 s | 23.2 |
| Warm | NPU | 14.8 s | 25.5 |
| Cold | CPU | 30.1 s | 23.8 |
| Cold | GPU | 9.6 s | 36.1 |
| Cold | NPU | 11.8 s | 27.2 |

Cold beat warm on GPU and NPU in that baseline, which is unexpected; re-check
it with the new runner.

## Security

1. **Who can sign in.** Google or Microsoft sign-in, checked on the server
   against the approved email list.
2. **What gets uploaded.** Size, dimension and type checks (by content, not
   file extension). Every image is decoded and re-saved as a clean PNG before
   the phone sees it, because the phone decodes images in native C++ code.
   Prompt length is capped; model and compute unit come from fixed lists.
3. **The phone.** Outgoing HTTPS only, no open ports, a revocable device key in
   app-private storage, and no debug hooks.

Also: per-user limits on queued work; secrets live in Vercel's settings and
never in git; competition images and data never go in the repo; every API
request carries a version number so an old app build fails clearly.

## Reliability

- If the phone stops reporting during a run, that work returns to the queue
  after a timeout.
- Work that crashes the app is marked failed after a few attempts, so it
  cannot crash the phone forever.
- The phone retries with backoff when the network drops; jobs wait in the queue
  meanwhile, and the website shows the phone as offline.

## Demo mode

Once the phone is returned, a fake worker replays recorded results so the site
still works. Demo mode must not depend on the live database, because free
Supabase projects pause after a week without activity. The same fake worker
runs end-to-end tests without the phone.

## Testing

- Server: pytest, including the existing 27 harness tests.
- Contract tests for the phone ↔ server API.
- End-to-end tests with the fake worker.
- GitHub Actions runs the server tests and the Android builds.

## Repository layout

Now:

- `android/` — Qualcomm's demo chat app and the current adb harness.
- `docs/` — this file.

Planned:

- `worker/` — the PhoneBench app (its exact project layout is settled in
  phase 1, part 4).
- `server/` — FastAPI, including the harness's Python code.
- `web/` — the React site.
- `supabase/` — database migrations and access rules.
- `config/` — the default job settings and their limits.
- `analysis/` — scoring and figure scripts for offline use.

## Build plan

Each phase is useful on its own; each part is reviewed before and after it is
built.

**Phase 1 — phone runner and local server**

1. Create the repo and import the app and harness. **Done** (`28482fc`).
2. Write `docs/API.md`: the phone ↔ server contract. **Done** (`b882685`).
3. Build the local FastAPI server, tested from the PC alone. **Done**
   (`5a34ff8`; 84 tests, plus an end-to-end run with the fake phone).
4. Build PhoneBench: service, polling, runs, model download, notification
   controls. **In progress:** built, installed and set up on the phone; it
   polls the server and took its first unit. Not yet verified: that unit's
   runs completing (it was still downloading Qwen3-VL-2B when work stopped).
5. Add the status screen.
6. Validate against the 2026-09-18 baseline and a small accuracy pilot
   (I016, L080, L104); test running alongside the chat app.
7. Remove the old UI-automation harness and debug hooks.

**Phase 2 — cloud backend.** Supabase tables, storage, access rules and the
job-claim function.

**Phase 3 — go live.** Deploy to Vercel, add sign-in and the allowlist, move
the phone to dorm Wi-Fi.

**Phase 4 — polish.** Results dashboard, demo mode, README with a diagram,
demo video. Later: an admin page, zip uploads, Drive and OneDrive folder
links.

## To verify

These are assumptions, not yet tested:

- Vercel's request size limit (believed to be a few MB) — the reason uploads
  go straight to Supabase storage.
- Supabase's free storage quota (believed to be about 1 GB) and whether the
  phone's polling keeps the free project from pausing.
- The GenieX SDK running in a background service with no app screen open.
- Android 16 and Samsung: starting the service at boot, polling for hours with
  the screen off, and Samsung's background app killing.
- `adb reverse` over wireless adb.
- Memory over hundreds of load and unload cycles in cold mode.
- Whether battery temperature and thermal state are enough to flag
  throttling.
- Whether a larger `max_edge` changes what the model sees. llama.cpp may
  resize images to the vision tower's own size, and the SDK reported only 63
  prompt tokens for a 768 px image, so image tokens may not be counted there.
- Whether the Qwen2.5-VL-7B bundle built for SM8750 runs on our SM8850.

## Notes for later

- **`max_edge` cap.** Left at 2048 for now. The phone's model context is
  limited (llama.cpp's `n_ctx` is sized from the vision tower), and larger
  images may cost more image tokens. Measure image tokens per size in part 6;
  lower the cap if larger images overflow the context.
- **Downscale at upload time.** Raw LPCVC images are 12–23 MB, so 100 of them
  would overflow the storage budget and Supabase's free tier before a job is
  submitted. Plan: choose `max_edge` before uploading and downscale each image
  as it arrives, deleting the original immediately. Needed before phase 3.
- **adb.** The phone can appear twice in `adb devices` (by IP and by its mDNS
  name); `adb disconnect <ip:port>` removes the duplicate.
- **Log noise.** While the model downloaded, Samsung's
  `PackageConfigPersister` logged a warning about twice a second for the app.
  Check whether notification updates cause it; throttle them if so.

## Open questions

- Does the dorm Wi-Fi require signing in again periodically? That would
  silently cut the phone off.
- Does the phone throttle when charging in a closed drawer?
- Which React build setup to use (decide in phase 4).
- Which framework comes after GenieX, and how it loads models.
- The TA's expected usage (remote submissions or local only) and whether the
  project can be public.

## Decisions

- 2026-09-24 — The phone runs jobs on its own. A PC worker was rejected
  because it needs a constant adb connection.
- 2026-09-25 — Standalone repo; FastAPI with React; plain polling; one job
  type; the settings file is the job format.
- 2026-09-25 — The worker runs headless in a background service, with a status
  screen and Pause, Stop and Resume.
- 2026-10-01 — The worker is a separate app (PhoneBench), not part of the demo
  chat app. Uninstalling it removes everything.
- 2026-10-01 — Model code sits behind an inference seam; results record the
  framework.
- 2026-10-01 — Google and Microsoft sign-in; everyone sees all results; admin
  settings for the queue.
- 2026-10-01 — Up to 100 images per job, uploaded as folders; subfolder names
  give labels; zip and cloud-drive links come later.
- 2026-10-01 — Jobs can compare all catalog vision models, several prompts
  and larger image sizes. Images are kept only while their job is active.
- 2026-10-01 — No admin page for now; limits are edited in Supabase's table
  editor. The GitHub repo stays private for now.
