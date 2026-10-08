"""Offline checks for the two-stage series runner, using a scripted stand-in for adb."""

import contextlib
import fnmatch
import hashlib
import io
import json
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import run_two_stage_series as runner
from run_two_stage_series import Collector, PACKAGE, build_series, clear_results, load_finished, push_file, thermal_sample
from two_stage import load_prompts

RESULTS = "files/benchmark/results"


def completed(response="{}", **extra):
    base = {"status": "completed", "response": response, "ttft_ms": 1000, "decode_time_ms": 2000,
            "generated_tokens": 20, "prompt_tokens": 300, "decode_tok_s": 10.0}
    base.update(extra)
    return base


class FakePhone:
    """Answers the adb calls the runner makes, from a small in-memory phone."""

    def __init__(self):
        self.results: dict[str, str] = {}
        self.files: dict[str, bytes] = {}      # private app storage, by relative path
        self.tmp: dict[str, bytes] = {}
        self.battery = "temperature: 305"
        self.thermal = "Thermal Status: 1"
        self.calls: list[tuple] = []
        self.rm_works = True

    def __call__(self, *args, timeout=30):
        self.calls.append(args)
        ok = lambda out="": SimpleNamespace(returncode=0, stdout=out, stderr="")
        fail = lambda err="": SimpleNamespace(returncode=1, stdout="", stderr=err)
        if args[:3] == ("shell", "run-as", PACKAGE):
            command = args[3:]
            if command[0] == "ls" and command[1] == RESULTS:
                return ok("\n".join(self.results))
            if command[0] == "cat" and command[1].startswith(RESULTS + "/"):
                return ok(self.results[command[1].rsplit("/", 1)[1]])
            if command[0] == "sha256sum":
                data = self.files.get(command[1])
                return ok(f"{hashlib.sha256(data).hexdigest()}  {command[1]}") if data is not None else fail("no such file")
            if command[0] == "sh":
                # Like real adb: the arguments are joined with spaces and parsed again on the phone,
                # so `sh -c` only gets the first word unless the whole script is quoted.
                words = shlex.split(" ".join(command))
                if words[:2] != ["sh", "-c"] or len(words) < 3:
                    return fail("sh: bad arguments")
                script = shlex.split(words[2])
                if script and script[0] == "rm" and self.rm_works:
                    for pattern in script[2:]:
                        folder, _, name_pattern = pattern.rpartition("/")
                        if folder == RESULTS:
                            for name in [n for n in self.results if fnmatch.fnmatch(n, name_pattern)]:
                                del self.results[name]
                return ok()
            if command[0] == "mkdir":
                return ok()
            if command[0] == "cp":
                self.files[command[2]] = self.tmp[command[1]]
                return ok()
        if args[0] == "push":
            self.tmp[args[2]] = Path(args[1]).read_bytes()
            return ok()
        if args[:3] == ("shell", "rm", "-f"):
            self.tmp.pop(args[3], None)
            return ok()
        if args[:3] == ("shell", "dumpsys", "battery"):
            return ok(self.battery)
        if args[:3] == ("shell", "dumpsys", "thermalservice"):
            return ok(self.thermal)
        raise AssertionError(f"Unscripted adb call: {args}")


def make_records(count=2):
    return [{"pair_id": f"I{n:03d}", "variant": "tampered", "remote_filename": f"img-{n:024d}.png",
             "prepared_size": [576, 768]} for n in range(1, count + 1)]


class SeriesTests(unittest.TestCase):
    def test_series_file_has_per_image_stage_two_with_the_pushed_size(self):
        records = make_records(2)
        records[1]["prepared_size"] = [768, 576]
        series = build_series(records, load_prompts(), "Qwen3-VL-2B-Instruct (GGUF)", "npu", 2.5, (256, 128))
        self.assertEqual((series["backend"], series["rest_ms"], series["max_tokens"]), ("NPU", 2500, {"stage1": 256, "stage2": 128}))
        self.assertEqual([item["index"] for item in series["items"]], [1, 2])
        self.assertEqual(series["items"][0]["image"], records[0]["remote_filename"])
        self.assertIn("576 pixels wide and 768 pixels tall", series["items"][0]["stage2"])
        self.assertIn("768 pixels wide and 576 pixels tall", series["items"][1]["stage2"])
        self.assertNotIn("{width}", json.dumps(series))
        self.assertEqual(series["stage1"], load_prompts()["stage1"])


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        self.phone = FakePhone()
        ticks = iter(range(0, 10_000, 10))
        self.collector = Collector(self.output, make_records(3), run=self.phone, clock=lambda: next(ticks))

    def rows(self):
        path = self.output / "iterations.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.is_file() else []

    def test_an_image_finishes_only_when_stage_two_arrives(self):
        self.phone.results["s0001-1.json"] = json.dumps(completed("one"))
        self.assertEqual(self.collector.poll(), 0)
        self.assertEqual(self.rows(), [])
        self.phone.battery = "temperature: 318"
        self.phone.results["s0001-2.json"] = json.dumps(completed("two"))
        self.assertEqual(self.collector.poll(), 1)
        row = self.rows()[0]
        self.assertEqual((row["iteration"], row["pair_id"], row["stage1"]["response"], row["stage2"]["response"]),
                         (1, "I001", "one", "two"))
        self.assertEqual((row["battery_c_before"], row["battery_c_after"]), (None, 31.8))
        self.assertTrue((self.output / "results" / "s0001-2.json").is_file())

    def test_polling_again_does_not_duplicate_finished_images(self):
        self.phone.results.update({"s0001-1.json": json.dumps(completed()), "s0001-2.json": json.dumps(completed())})
        self.collector.poll()
        self.collector.poll()
        self.assertEqual(len(self.rows()), 1)

    def test_a_failed_stage_one_finishes_the_image_without_stage_two(self):
        self.phone.results["s0002-1.json"] = json.dumps({"status": "error", "error": "out of memory", "response": ""})
        self.assertEqual(self.collector.poll(), 1)
        row = self.rows()[0]
        self.assertEqual((row["iteration"], row["stage1"]["status"], row["stage2"]), (2, "error", None))

    def test_half_written_results_are_retried_on_the_next_poll(self):
        self.phone.results["s0001-1.json"] = '{"status": "comp'
        self.collector.poll()
        self.assertEqual(self.collector.stages, {})
        self.phone.results["s0001-1.json"] = json.dumps(completed())
        self.phone.results["s0001-2.json"] = json.dumps(completed())
        self.assertEqual(self.collector.poll(), 1)

    def test_final_poll_closes_an_image_that_never_got_stage_two(self):
        self.phone.results["s0003-1.json"] = json.dumps(completed())
        self.assertEqual(self.collector.poll(), 0)
        self.assertEqual(self.collector.poll(final=True), 1)
        self.assertIsNone(self.rows()[0]["stage2"])

    def test_warmup_and_unrelated_files_are_ignored(self):
        self.phone.results.update({"warmup-1.json": json.dumps(completed()), "notes.txt": "x"})
        self.assertEqual(self.collector.poll(), 0)
        self.assertEqual(self.collector.stages, {})

    def test_every_poll_records_a_temperature_sample(self):
        self.collector.poll()
        self.collector.poll()
        samples = [json.loads(line) for line in (self.output / "thermal.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(samples), 2)
        self.assertEqual((samples[0]["battery_c"], samples[0]["thermal_status"]), (30.5, 1))
        self.assertGreater(samples[1]["t_s"], samples[0]["t_s"])

    def test_resume_skips_images_already_recorded(self):
        self.phone.results.update({"s0001-1.json": json.dumps(completed()), "s0001-2.json": json.dumps(completed())})
        self.collector.poll()
        self.assertEqual(load_finished(self.output), {1})
        again = Collector(self.output, make_records(3), run=self.phone)
        again.adopt(load_finished(self.output))
        self.assertEqual(again.poll(), 0)
        self.assertEqual(len(self.rows()), 1)


class EarlierRunTests(unittest.TestCase):
    """A second run on the same phone must never read the first run's result files."""

    def phone_with_old_results(self):
        phone = FakePhone()
        phone.results.update({"s0001-1.json": json.dumps(completed(model="A")), "s0001-2.json": json.dumps(completed(model="A")),
                              "warmup-1.json": json.dumps(completed()), "notes.txt": "keep"})
        return phone

    def test_clear_removes_earlier_results_and_nothing_else(self):
        phone = self.phone_with_old_results()
        clear_results(phone)
        self.assertEqual(sorted(phone.results), ["notes.txt"])

    def test_clear_refuses_to_go_on_when_the_files_are_still_there(self):
        phone = self.phone_with_old_results()
        phone.rm_works = False
        with self.assertRaisesRegex(RuntimeError, "Could not clear"):
            clear_results(phone)

    def test_a_result_from_another_model_stops_the_run(self):
        phone = self.phone_with_old_results()
        with tempfile.TemporaryDirectory() as directory:
            collector = Collector(Path(directory), make_records(2), run=phone, expected_model="B")
            with self.assertRaisesRegex(RuntimeError, "left over from another run"):
                collector.poll()
            self.assertEqual(collector.stages, {})

    def test_results_from_the_expected_model_are_accepted(self):
        phone = self.phone_with_old_results()
        with tempfile.TemporaryDirectory() as directory:
            collector = Collector(Path(directory), make_records(2), run=phone, expected_model="A")
            self.assertEqual(collector.poll(), 1)


class ThermalAndPushTests(unittest.TestCase):
    def test_missing_thermal_service_still_gives_a_battery_reading(self):
        phone = FakePhone()
        phone.thermal = "no such service"
        self.assertEqual(thermal_sample(phone), {"battery_c": 30.5, "thermal_status": None})

    def test_push_verifies_hashes_and_skips_identical_files(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "a.png"
            local.write_bytes(b"pixels")
            phone = FakePhone()
            self.assertTrue(push_file(local, "img-a.png", phone))
            self.assertEqual(phone.files["files/benchmark/img-a.png"], b"pixels")
            self.assertEqual(phone.tmp, {})  # staging copy removed
            self.assertFalse(push_file(local, "img-a.png", phone))
            local.write_bytes(b"changed")
            self.assertTrue(push_file(local, "img-a.png", phone))
            self.assertEqual(phone.files["files/benchmark/img-a.png"], b"changed")

    def test_push_rejects_unsafe_names_and_corrupt_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            local = Path(directory) / "a.png"
            local.write_bytes(b"pixels")
            with self.assertRaises(ValueError):
                push_file(local, "../escape.png", FakePhone())

            class Corrupting(FakePhone):
                def __call__(self, *args, timeout=30):
                    result = super().__call__(*args, timeout=timeout)
                    if args[:4] == ("shell", "run-as", PACKAGE, "cp"):
                        self.files[args[5]] = b"garbage"
                    return result

            with self.assertRaises(RuntimeError):
                push_file(local, "img-a.png", Corrupting())


class DryRunTests(unittest.TestCase):
    def manifest(self, directory: Path) -> Path:
        records = []
        for n in range(1, 5):
            for variant, label in (("original", "real"), ("tampered", "tampered")):
                records.append({"pair_id": f"I{n:03d}", "variant": variant, "expected_label": label,
                                "source_kind": "nano_banana" if variant == "tampered" else None,
                                "edit": "add" if variant == "tampered" else None,
                                "remote_filename": f"img-{n:02d}{variant[0]}.png", "prepared_size": [576, 768]})
        path = directory / "manifest.json"
        path.write_text(json.dumps({"kind": "tamper_comparison", "records": records, "pairs": []}), encoding="utf-8")
        return path

    def run_main(self, *argv):
        out = io.StringIO()
        with patch.object(sys, "argv", ["run_two_stage_series.py", *argv]), contextlib.redirect_stdout(out):
            code = runner.main()
        return code, out.getvalue()

    def test_dry_run_writes_the_series_and_never_calls_adb(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, "adb", side_effect=AssertionError("adb called")):
            root = Path(directory)
            code, text = self.run_main("--manifest", str(self.manifest(root)), "--output", str(root / "run"),
                                       "--model", "Qwen3-VL-2B-Instruct (GGUF)", "--count", "4", "--dry-run")
            series = json.loads((root / "run" / "series.json").read_text(encoding="utf-8"))
            settings = json.loads((root / "run" / "run-settings.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(len(series["items"]), 4)
        self.assertEqual((settings["runtime"], settings["label"]), ("llama_cpp", "llama_cpp-npu"))
        self.assertIn("4 images (2 tampered)", text)

    def test_unknown_model_and_changed_settings_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = str(self.manifest(root))
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.run_main("--manifest", manifest, "--output", str(root / "a"), "--model", "No Such Model", "--dry-run")
                self.run_main("--manifest", manifest, "--output", str(root / "b"), "--model", "Qwen3-VL-2B-Instruct (GGUF)",
                              "--count", "4", "--dry-run")
                with self.assertRaises(SystemExit):
                    self.run_main("--manifest", manifest, "--output", str(root / "b"), "--model", "Qwen3-VL-2B-Instruct (GGUF)",
                                  "--count", "4", "--seed", "9", "--dry-run")


if __name__ == "__main__":
    unittest.main()
