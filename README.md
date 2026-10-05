# LPCVC PhoneBench

Run vision-language model benchmarks on a Snapdragon phone from anywhere. A
background worker app on the phone takes jobs from a server, runs them on the
CPU, GPU, or NPU with the Qualcomm GenieX SDK, and reports results back.

Status: early development. The phone used for this project is rented, so the
live worker is only available while I have it.

**To use it, start with [docs/USAGE.md](docs/USAGE.md).**

## Layout

- `docs/USAGE.md` — setup, running jobs, settings, results, troubleshooting.
- `docs/DESIGN.md` — the design: architecture, job format, phone app,
  security, build plan and notes for later. `docs/API.md` — the phone ↔
  server API.
- `server/` — the job server (FastAPI). See [server/README.md](server/README.md).
- `worker/` — the PhoneBench Android app. See [worker/README.md](worker/README.md).
- `config/` — default job settings, limits and the vision model list.
- `supabase/migrations/` — the cloud database tables, access rules and
  storage bucket, applied to the Supabase project.
- `android/` — Qualcomm's GenieX demo chat app with the current adb-driven
  benchmark and accuracy-pilot harness. See
  [android/BENCHMARK.md](android/BENCHMARK.md).

## Origin

`android/` was imported from the `geniex_chat_android` sample in
[quic/ai-hub-apps](https://github.com/quic/ai-hub-apps) v0.37.2, together with
LPCVC benchmark work built on top of it (commit `efca78b`). That code is
BSD-3-Clause licensed; see [LICENSE](LICENSE). The GenieX SDK is licensed under
Apache 2.0.
