# LPCVC PhoneBench — phone ↔ server API

Status: draft for review, 2026-10-01 (phase 1, part 2). This is the contract
between the PhoneBench app and the server. The same API is served by the local
server (phase 1) and the cloud server (phase 3), so the phone code does not
change between them. See [DESIGN.md](DESIGN.md) for the why.

## Concepts

- **Job** — what a user submits: models × compute units × images × prompts ×
  repetitions, plus settings. States: `queued` → `running` → `done`, `failed`
  or `cancelled`.
- **Unit** — the piece of a job the phone takes at once: **one model on one
  compute unit**, with an ordered list of runs. Keeping a unit to one model
  and one compute unit lets warm mode load once and do one warm-up.
- **Run** — one image + one prompt + one repetition. The phone reports each
  run as soon as it finishes.

The server splits a job into units in the order the job lists models and
compute units. Within a unit, runs go image by image, then prompt, then
repetition.

## Conventions

- Base path: `/api/v1`. A breaking change gets `/api/v2`.
- JSON bodies, UTF-8. Times are ISO 8601 in UTC (`2026-10-01T18:04:05Z`).
- IDs are opaque strings chosen by the server (`j_…`, `u_…`, `r_…`, `img_…`,
  `p_…`). The phone never builds IDs or URLs itself.
- The phone sends `PhoneBench-App-Version: 0.1.0` on every request. If the
  app is too old, the server answers `426` with the minimum version.
- Errors look like `{"error": {"code": "lease_lost", "message": "…"}}`.

## Authentication

- **Phone endpoints** (`/api/v1/device/…`) need
  `Authorization: Bearer <device key>`. The server stores only a hash of the
  key; the admin can revoke it. The phone keeps the key in app-private
  storage.
- **User endpoints** need a signed-in session in the cloud. The local server
  listens only on `127.0.0.1` and has no sign-in.

## Phone endpoints

### Poll — `POST /api/v1/device/poll`

One call is both the heartbeat and the request for work. The phone polls
every `poll_after_s` seconds **at all times**, including while running a unit,
so the server always knows it is alive and can tell it to stop.

Request:

```json
{
  "state": "idle",
  "unit_id": null,
  "app_version": "0.1.0",
  "framework": "geniex",
  "framework_version": "0.3.5",
  "models_downloaded": ["Qwen3-VL-2B-Instruct-GGUF"],
  "battery_c": 31.5,
  "thermal_status": "none",
  "free_memory_mb": 5800
}
```

- `state`: `idle`, `downloading`, `running`, `paused` or `error`.
- `unit_id`: the unit being worked on, or `null`.
- `thermal_status`: Android's thermal status name (`none`, `light`,
  `moderate`, `severe`, …).

Response:

```json
{
  "poll_after_s": 10,
  "cancel_unit": false,
  "unit": null
}
```

- The server assigns a unit only when `state` is `idle` and the queue is not
  paused. Otherwise `unit` is `null`.
- `cancel_unit: true` means stop the current unit now: stop generation,
  delete its images, and call [Finish](#finish--post-apiv1deviceunitsunit_idfinish)
  with `cancelled`.

A unit:

```json
{
  "unit_id": "u_8f2c",
  "job_id": "j_41aa",
  "job_name": "qwen-2b-vs-4b",
  "submitted_by": "teammate@purdue.edu",
  "lease_s": 180,
  "framework": "geniex",
  "model": "Qwen3-VL-2B-Instruct-GGUF",
  "backend": "NPU",
  "mode": "warm",
  "warmup": true,
  "rest_seconds": 10,
  "max_tokens": 2048,
  "run_timeout_s": 600,
  "images": [
    {"image_id": "img_01", "url": "/api/v1/device/images/img_01", "sha256": "9b1e…", "bytes": 348003}
  ],
  "prompts": [
    {"prompt_id": "p_01", "text": "Can you identify if this image is real, full synthetic, or tampered? …"}
  ],
  "runs": [
    {"run_id": "r_0001", "image_id": "img_01", "prompt_id": "p_01", "repetition": 1},
    {"run_id": "r_0002", "image_id": "img_01", "prompt_id": "p_01", "repetition": 2}
  ]
}
```

- `model` is an `id` from the app's model catalog.
- `runs` lists only the runs still to do. A unit that resumes after an
  interruption simply has fewer runs.

### Image — `GET /api/v1/device/images/{image_id}`

Returns the prepared PNG. Only images in a unit currently leased to this phone
can be fetched. The phone checks the SHA-256 before using it.

### Run report — `POST /api/v1/device/runs/{run_id}`

Sent once per run, right after it finishes.

```json
{
  "unit_id": "u_8f2c",
  "status": "completed",
  "started_at": "2026-10-01T18:04:05Z",
  "finished_at": "2026-10-01T18:05:21Z",
  "response": "The image appears to be real …",
  "error": null,
  "metrics": {
    "ttft_ms": 14826.3,
    "prompt_tokens": 63,
    "generated_tokens": 157,
    "prefill_tok_s": 4.26,
    "decode_tok_s": 25.45,
    "stop_reason": "eos"
  },
  "load_ms": null,
  "warm": true,
  "framework": "geniex",
  "framework_version": "0.3.5",
  "runtime": "llama_cpp",
  "compute_unit": "NPU",
  "battery_c": 33.0,
  "thermal_status": "light",
  "free_memory_mb": 3100
}
```

- `status`: `completed` or `error` (with `error` set and `metrics` null).
- `load_ms`: set on the run that loaded the model (every run in cold mode,
  the first in warm mode), else `null`.
- The server computes `reported_total_tokens` and scoring; the phone sends
  raw numbers only.
- Reporting the same `run_id` twice is harmless: the server keeps the first.

Response: `{"continue": true}`. `false` means the unit was cancelled; stop and
call Finish with `cancelled`.

### Finish — `POST /api/v1/device/units/{unit_id}/finish`

```json
{"status": "done", "error": null}
```

| Status | When | Server does |
|---|---|---|
| `done` | Every run was reported | Marks the unit done |
| `released` | Pause was pressed; the current run finished | Requeues the remaining runs; not counted as a failure |
| `cancelled` | Stop was pressed on the phone, or the server said cancel | Cancels the whole job |
| `failed` | Something unrecoverable: model download or load failed, a check failed | Requeues the remaining runs and counts an attempt |

After Finish, the phone deletes the unit's images and goes back to `idle` (or
stays `paused`).

## Leases and recovery

- Assigning a unit starts a lease of `lease_s` (180 s). Every poll that names
  the unit, and every run report, renews it.
- If the lease runs out, the phone is presumed dead: the remaining runs go
  back in the queue as a new unit. Later calls about the old unit get `409
  lease_lost`, and the phone abandons it and deletes its images.
- **Crash loops.** The run after the last reported one is the one that was in
  progress. If the phone dies during the same run twice, that run is
  recorded as an error ("worker stopped during this run twice") and skipped.
- A unit's remaining runs are retried at most 3 times after `failed`; after
  that they are recorded as errors and the job ends `failed`.

## Phone behavior

**Running a unit**

- Warm mode: load the model, do one warm-up run with the first run's image
  and prompt (not reported), then for each run: reset the context, run,
  report, rest `rest_seconds` (not after the last run).
- Cold mode: for each run: load, run, unload, report, rest.
- A run that exceeds `run_timeout_s` is stopped and reported as an error.
- If the model is not downloaded, download it first with `state:
  "downloading"`; polling continues, so the lease stays alive.

**Checks before running.** The phone re-checks every unit and finishes it
`failed` if any check fails:

- the model is a vision model in the app's catalog, and `backend` is allowed
  for it;
- `repetitions`, `rest_seconds`, `max_tokens`, image and prompt counts are
  within the limits in DESIGN.md;
- every image URL is on the same server as the API (the phone never fetches
  from anywhere else), its SHA-256 matches, and it decodes as a PNG of at most
  4096 px.

**Errors and retries**

| Response | Phone does |
|---|---|
| Network error or `5xx` | Retry with backoff: 2 s, 4 s, 8 s … up to 60 s. Keep running the current unit and hold its reports until they go through. |
| `401` | Stop polling; status screen shows "device key rejected". |
| `409 lease_lost` | Abandon the unit, delete its images, go idle. |
| `426` | Stop polling; status screen shows "update the app". |
| `400` / `422` | A bug: log it, finish the unit `failed`. |

## User endpoints (summary)

Used by the website and, in phase 1, by a small command-line tool on the PC.
Full details come with phase 2.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/uploads` | Upload images; returns image IDs. In the cloud the browser uploads to Supabase storage and this call registers them. |
| `POST /api/v1/jobs` | Submit a job: a name plus settings (see DESIGN.md, Job format) |
| `GET /api/v1/jobs` | List jobs with progress |
| `GET /api/v1/jobs/{job_id}` | One job: status, progress, per-run results |
| `POST /api/v1/jobs/{job_id}/cancel` | Cancel a job |
| `GET /api/v1/jobs/{job_id}/outputs/{file}` | `runs.csv`, `summary.json`, accuracy reports |
| `GET /api/v1/device/status` | Whether the phone is online, paused or busy, and when it was last seen |

## Local mode (phase 1)

- The server runs on the PC at `http://127.0.0.1:8000`.
- `adb reverse tcp:8000 tcp:8000` makes the phone's `127.0.0.1:8000` reach
  the PC, over USB or wireless adb.
- The app allows plain HTTP only to `127.0.0.1`, and only in debug builds;
  everything else must be HTTPS.
- The server creates a device key on first start and prints it; it is set on
  the phone over adb.
- Job outputs are written to a folder on the PC in the same layout as the
  current harness.

## Example

1. A user submits job `j_41aa`: Qwen3-VL-2B and Qwen3-VL-4B on NPU, 2 images,
   1 prompt, 10 repetitions, warm. The server makes two units of 20 runs each.
2. The phone polls with `idle` and receives unit `u_8f2c` (2B on NPU).
3. It downloads and checks both images, loads the model, does the warm-up,
   then runs and reports `r_0001` … `r_0020`, polling every 10 s throughout.
4. It calls Finish with `done`, deletes the images and polls with `idle`.
5. It receives the 4B unit, downloads that model first (`downloading`), and
   repeats.
6. When both units are done, the job is `done` and its outputs are ready.

## Open points for review

- Lease length (180 s) and poll interval (10 s).
- Default `max_tokens` (2048, as in the current app).
- Whether Stop on the phone should cancel the whole job (as written) or only
  release the current unit.
- Run order within a unit (image, then prompt, then repetition).
