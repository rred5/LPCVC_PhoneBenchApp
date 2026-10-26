"""A simulated phone that speaks the API in docs/API.md.

Used for end-to-end tests without the phone, and later for demo mode. It
follows the same rules as the real app: poll, check images, run, report each
run, finish.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import random

import httpx

RESPONSES = {
    "real": "The image appears to be real, with consistent lighting and no visible edits.",
    "synthetic": "This image looks AI-generated; the textures are too smooth to be a photograph.",
    "tampered": "The image has been tampered with: the object on the left was pasted in.",
}
APP_VERSION = "0.3.0"


class FakeWorker:
    def __init__(self, client: httpx.Client, key: str, seed: int = 0, app_version: str = APP_VERSION):
        self.client = client
        self.headers = {"Authorization": f"Bearer {key}", "PhoneBench-App-Version": app_version}
        self.random = random.Random(seed)
        self.clock = datetime(2026, 10, 1, tzinfo=timezone.utc)

    def poll(self, state: str = "idle", unit_id: str | None = None) -> httpx.Response:
        return self.client.post(
            "/api/v1/device/poll",
            headers=self.headers,
            json={
                "state": state,
                "unit_id": unit_id,
                "app_version": APP_VERSION,
                "framework": "geniex",
                "framework_version": "fake",
                "models_downloaded": [],
                "battery_c": 31.0,
                "thermal_status": "none",
                "free_memory_mb": 6000,
            },
        )

    def fetch_images(self, unit: dict) -> None:
        for image in unit["images"]:
            response = self.client.get(image["url"], headers=self.headers)
            response.raise_for_status()
            if hashlib.sha256(response.content).hexdigest() != image["sha256"]:
                raise AssertionError(f"image {image['image_id']} failed its SHA-256 check")

    def result(self, unit: dict, run: dict, first: bool) -> dict:
        """A plausible run report; the response names a random label."""
        started = self.clock
        self.clock += timedelta(seconds=self.random.uniform(40, 90))
        label = self.random.choice(list(RESPONSES))
        return {
            "unit_id": unit["unit_id"],
            "status": "completed",
            "started_at": started.isoformat().replace("+00:00", "Z"),
            "finished_at": self.clock.isoformat().replace("+00:00", "Z"),
            "response": RESPONSES[label],
            "error": None,
            "metrics": {
                "ttft_ms": round(self.random.uniform(9_000, 30_000), 1),
                "prompt_tokens": 63,
                "generated_tokens": self.random.randint(50, 600),
                "prefill_tok_s": round(self.random.uniform(2, 7), 2),
                "decode_tok_s": round(self.random.uniform(20, 36), 2),
                "stop_reason": "eos",
            },
            "load_ms": round(self.random.uniform(2_000, 6_000)) if first or unit["mode"] == "cold" else None,
            "warm": unit["mode"] == "warm",
            "framework": "geniex",
            "framework_version": "fake",
            "runtime": "llama_cpp",
            "compute_unit": unit["backend"],
            "battery_c": 32.0,
            "thermal_status": "none",
            "free_memory_mb": 5000,
            **self.follow_ups(unit, run, RESPONSES[label]),
        }

    def follow_ups(self, unit: dict, run: dict, first_response: str) -> dict:
        """Per-turn results for a multi-turn prompt; nothing for a single turn."""
        prompt = next(item for item in unit["prompts"] if item["prompt_id"] == run["prompt_id"])
        turns = prompt.get("turns") or [prompt["text"]]
        if len(turns) < 2:
            return {}
        reports = [{"response": first_response, "error": None, "metrics": {"ttft_ms": 900.0, "prompt_tokens": 500, "generated_tokens": 60, "decode_tok_s": 25.0}}]
        for number in range(2, len(turns) + 1):
            reports.append(
                {"response": f'{{"turn": {number}, "boxes": []}}', "error": None,
                 "metrics": {"ttft_ms": 150.0, "prompt_tokens": 120, "generated_tokens": 10, "decode_tok_s": 25.0}}
            )
        return {"turns": reports}

    def run_unit(self, unit: dict, stop_after: int | None = None) -> str:
        """Run a unit; return how it finished. `stop_after` simulates Pause after N runs."""
        self.fetch_images(unit)
        for index, run in enumerate(unit["runs"]):
            if stop_after is not None and index == stop_after:
                self.finish(unit, "released")
                return "released"
            response = self.client.post(
                f"/api/v1/device/runs/{run['run_id']}", headers=self.headers, json=self.result(unit, run, index == 0)
            )
            response.raise_for_status()
            if not response.json()["continue"]:
                self.finish(unit, "cancelled")
                return "cancelled"
        self.finish(unit, "done")
        return "done"

    def finish(self, unit: dict, status: str, error: str | None = None) -> None:
        response = self.client.post(
            f"/api/v1/device/units/{unit['unit_id']}/finish", headers=self.headers, json={"status": status, "error": error}
        )
        response.raise_for_status()

    def take_unit(self) -> dict | None:
        response = self.poll("idle")
        response.raise_for_status()
        return response.json()["unit"]

    def run_until_idle(self, max_units: int = 100) -> int:
        """Take and run units until the queue is empty; return how many were run."""
        for count in range(max_units):
            unit = self.take_unit()
            if unit is None:
                return count
            self.run_unit(unit)
        return max_units
