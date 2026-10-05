# PhoneBench app

The Android worker from [docs/API.md](../docs/API.md): a background service
that takes units from the server, runs them on the CPU, GPU or NPU with the
Qualcomm GenieX SDK, and reports every run. Setup and day-to-day use are in
[docs/USAGE.md](../docs/USAGE.md).

- App ID `com.rred5.phonebench`, version 0.2.0; Android 12 (API 31) or newer.
  The server requires 0.2.0 or newer.
- Dependencies: GenieX SDK 0.3.5 (Apache 2.0) and `androidx.core`. Network
  and JSON use Android's built-in classes.

## Build and install

```powershell
$env:JAVA_HOME = 'C:\Program Files\Android\Android Studio\jbr'
.\gradlew.bat assembleDebug
adb install -r build\outputs\apk\debug\phonebench-debug.apk
```

## Code

| File | What it does |
|---|---|
| `WorkerService.kt` | Foreground service: polls the server, runs units one at a time, notification with Pause / Resume / Stop job |
| `UnitRunner.kt` | Runs one unit: checks it, downloads and verifies images, downloads the model if needed, warm or cold runs with a per-run timeout, reports each run (retrying through network drops), heartbeats, Pause / Stop / Quit / cancel |
| `inference/InferenceEngine.kt` | The seam every framework implements (load, reset, run, stop, unload, download) |
| `inference/GenieXEngine.kt` | GenieX implementation; load settings and the generation call follow the demo app in `android/` |
| `inference/GgufVisionConfig.kt` | Reads a model's vision-tower size to size its context (copied from the demo app) |
| `api/ApiClient.kt`, `api/WorkUnit.kt` | HTTP calls, unit parsing, and the checks the phone applies before running a unit |
| `SetupReceiver.kt` | adb-only setup of the server address and device key |
| `BootReceiver.kt` | Restarts the worker after a reboot unless it was quit |
| `MainActivity.kt` | Minimal controls; the full status screen is phase 1, part 5 |
| `assets/models.json` | Models this app can download; IDs match `config/models.json` |

## Behavior worth knowing

- Every unit is checked before it runs: the model and compute unit, all
  limits, and every image (only requested from the server's own image path,
  SHA-256 matched, a PNG of at most 4096 px).
- A cloud server answers image requests with a redirect to file storage. The
  app follows exactly one redirect, only to an https URL, and sends no device
  key there (0.2.0 and newer).
- Warm units load the model once, run one unreported warm-up, and reset the
  context before each run; cold units load and unload for every run. The
  model is unloaded after every unit.
- A run cut short by Stop, Quit or a server cancel is never reported as a
  measurement.
- Debug builds may use plain HTTP to `127.0.0.1` only; release builds require
  HTTPS.
- The app writes nothing outside its private storage; uninstalling removes
  everything, including downloaded models.
