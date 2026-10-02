"""Check uploaded images and prepare them the same way as the adb harness.

`prepare` follows android/prepare_benchmark_image.py step for step, so results
stay comparable with the 2026-09-18 baselines.
"""

from dataclasses import dataclass
import hashlib
import io
from pathlib import PurePosixPath

import PIL
from PIL import Image, ImageOps, UnidentifiedImageError

LABELS = ("real", "synthetic", "tampered")
ALLOWED_FORMATS = {"PNG", "JPEG", "WEBP"}
MAX_NAME_CHARS = 255


class ImageError(ValueError):
    """An upload is not an acceptable image; the message says why."""


@dataclass(frozen=True)
class CheckedImage:
    format: str
    width: int
    height: int
    bytes: int
    sha256: str


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def clean_name(raw: str) -> str:
    """Normalize a path from an uploaded folder; reject anything that could escape it."""
    if not isinstance(raw, str):
        raise ImageError("missing file name")
    name = raw.replace("\\", "/").strip()
    while name.startswith("./"):
        name = name[2:]
    parts = PurePosixPath(name).parts
    if (
        not name
        or len(name) > MAX_NAME_CHARS
        or name.startswith("/")
        or (parts and ":" in parts[0])
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(char) < 32 for char in name)
    ):
        raise ImageError("file name is not a plain relative path")
    return "/".join(parts)


def label_for(name: str) -> str | None:
    """The label of an image in a `real/`, `synthetic/` or `tampered/` folder."""
    parts = PurePosixPath(name).parts
    if len(parts) < 2:
        return None
    folder = parts[-2].lower()
    return folder if folder in LABELS else None


def check_upload(data: bytes, max_bytes: int, max_edge: int) -> CheckedImage:
    """Accept only a complete, single-frame PNG, JPEG or WebP within the limits."""
    if len(data) > max_bytes:
        raise ImageError(f"file is larger than {max_bytes // (1024 * 1024)} MB")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
            frames = getattr(image, "n_frames", 1)
        # Check the header before decoding anything large.
        if image_format not in ALLOWED_FORMATS:
            raise ImageError(f"{image_format or 'unknown'} images are not accepted; use PNG, JPEG or WebP")
        if max(width, height) > max_edge:
            raise ImageError(f"image is {width}x{height}; the long edge must be at most {max_edge} px")
        if frames != 1:
            raise ImageError("animated images are not accepted")
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    except ImageError:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, SyntaxError, ValueError) as error:
        raise ImageError("not a readable image") from error
    return CheckedImage(image_format, width, height, len(data), sha256(data))


def prepare(data: bytes, max_edge: int) -> tuple[bytes, dict]:
    """Return an aspect-preserving RGB PNG with the long edge at most `max_edge`."""
    with Image.open(io.BytesIO(data)) as image:
        source_size = image.size
        image = ImageOps.exif_transpose(image)
        # Flatten alpha consistently; change this background only for a new experiment.
        if "A" in image.getbands() or "transparency" in image.info:
            rgba = image.convert("RGBA")
            image = Image.alpha_composite(Image.new("RGBA", rgba.size, "white"), rgba)
        image = image.convert("RGB")
        scale = min(1.0, max_edge / max(image.size))
        target = tuple(max(1, round(edge * scale)) for edge in image.size)
        if image.size != target:
            image = image.resize(target, Image.Resampling.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
    output = buffer.getvalue()
    with Image.open(io.BytesIO(output)) as check:
        check.load()
        if check.format != "PNG" or check.mode != "RGB" or check.size != target:
            raise ImageError("prepared image failed its self-check")
    return output, {
        "source_sha256": sha256(data),
        "source_size": list(source_size),
        "output_sha256": sha256(output),
        "output_size": list(target),
        "max_edge": max_edge,
        "resampling": "Lanczos",
        "crop": False,
        "upscale": False,
        "exif_orientation_applied": True,
        "alpha_background": "white",
        "mode": "RGB",
        "format": "PNG",
        "pillow_version": PIL.__version__,
    }
