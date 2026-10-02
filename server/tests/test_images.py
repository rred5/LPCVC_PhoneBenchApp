import hashlib
import io
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest

from phonebench import images
from phonebench.config import REPO_ROOT
from tests.conftest import image_bytes

HARNESS_PREPARE = REPO_ROOT / "android" / "prepare_benchmark_image.py"


def _noise_png() -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise((400, 400), 64).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


NOISE_PNG = _noise_png()


@pytest.mark.parametrize(
    "raw, clean",
    [("tampered\\L106.png", "tampered/L106.png"), ("./real/a.png", "real/a.png"), ("a.png", "a.png")],
)
def test_clean_name(raw, clean):
    assert images.clean_name(raw) == clean


@pytest.mark.parametrize("raw", ["", "../x.png", "/etc/passwd", "C:/x.png", "a/../b.png", "a\x00b.png", "x" * 300])
def test_clean_name_rejects(raw):
    with pytest.raises(images.ImageError):
        images.clean_name(raw)


@pytest.mark.parametrize(
    "name, label",
    [("set/tampered/x.png", "tampered"), ("REAL/x.png", "real"), ("synthetic/x.png", "synthetic"), ("x.png", None), ("other/x.png", None)],
)
def test_label_from_folder(name, label):
    assert images.label_for(name) == label


def test_accepts_png_jpeg_webp():
    for fmt in ("PNG", "JPEG", "WEBP"):
        checked = images.check_upload(image_bytes(fmt=fmt), 10_000_000, 4096)
        assert (checked.format, checked.width, checked.height) == (fmt, 64, 48)


@pytest.mark.parametrize(
    "data, limit, edge, reason",
    [
        (b"hello, not an image", 10_000_000, 4096, "not a readable image"),
        (image_bytes(), 100, 4096, "larger than"),
        (image_bytes(width=5000, height=10), 10_000_000, 4096, "long edge"),
        (image_bytes(fmt="GIF"), 10_000_000, 4096, "not accepted"),
        (NOISE_PNG[: len(NOISE_PNG) // 2], 10_000_000, 4096, "not a readable image"),
    ],
    ids=["not-an-image", "too-many-bytes", "too-wide", "gif", "truncated"],
)
def test_rejects_bad_uploads(data, limit, edge, reason):
    with pytest.raises(images.ImageError, match=reason):
        images.check_upload(data, limit, edge)


def _decode(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


def test_prepare_downscales_without_cropping_or_upscaling():
    assert _decode(images.prepare(image_bytes(1600, 900), 768)[0]).size == (768, 432)
    assert _decode(images.prepare(image_bytes(300, 200), 768)[0]).size == (300, 200)


def test_prepare_flattens_transparency_onto_white():
    output = _decode(images.prepare(image_bytes(mode="RGBA", color=(0, 0, 0, 0)), 768)[0])
    assert output.mode == "RGB" and output.getpixel((0, 0)) == (255, 255, 255)


def test_prepare_applies_exif_rotation():
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees
    output = _decode(images.prepare(image_bytes(80, 40, fmt="JPEG", exif=exif), 768)[0])
    assert output.size == (40, 80)


def test_prepare_matches_the_harness_byte_for_byte(tmp_path: Path):
    source = tmp_path / "sample.png"
    gradient = Image.linear_gradient("L").convert("RGB").resize((1200, 900))
    gradient.save(source)
    subprocess.run([sys.executable, str(HARNESS_PREPARE), str(source), "--max-edge", "768"], check=True, capture_output=True)
    harness = (tmp_path / "prepared_768" / "sample-768.png").read_bytes()
    ours, meta = images.prepare(source.read_bytes(), 768)
    assert hashlib.sha256(ours).hexdigest() == hashlib.sha256(harness).hexdigest() == meta["output_sha256"]
