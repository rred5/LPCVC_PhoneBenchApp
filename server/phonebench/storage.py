"""File storage: a local folder in local mode, a private Supabase bucket in cloud mode.

Keys look like `incoming/<upload_id>` (an original, until it is prepared) and
`images/<upload_id>.png` (a prepared image, until its job is done).

Large files never pass through the server's own requests in cloud mode,
because the host caps request and response bodies at a few MB. Browsers
upload originals straight to the bucket with a one-time signed URL, and the
phone downloads prepared images from a short-lived signed URL.
"""

from pathlib import Path
from urllib.parse import quote

import httpx

SIGNED_URL_SECONDS = 300
# Below this size the server returns a file itself; above it, cloud mode
# hands out a signed URL instead.
INLINE_BYTES = 4_000_000


class FileTooLarge(Exception):
    pass


class UpstreamError(RuntimeError):
    """Supabase answered with an error."""


class LocalStorage:
    """The bucket as a folder; signed URLs are not needed, so there are none."""

    cloud = False

    def __init__(self, root: Path):
        self.root = root

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"bad storage key {key!r}")
        return path

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(data)
        temporary.replace(path)

    def get(self, key: str, max_bytes: int) -> bytes | None:
        """The file's bytes, or None if there is no such file."""
        path = self._path(key)
        if not path.is_file():
            return None
        if path.stat().st_size > max_bytes:
            raise FileTooLarge(key)
        return path.read_bytes()

    def delete(self, keys: list[str]) -> None:
        for key in keys:
            self._path(key).unlink(missing_ok=True)

    def upload_url(self, key: str) -> str | None:
        return None

    def download_url(self, key: str, filename: str | None = None) -> str | None:
        return None


class SupabaseStorage:
    """A private Supabase Storage bucket, used with the project's secret key."""

    cloud = True

    def __init__(self, url: str, key: str, bucket: str, client: httpx.Client | None = None):
        self.base = f"{url}/storage/v1"
        self.bucket = bucket
        headers = {"apikey": key}
        # Legacy service-role keys are JWTs and also go in Authorization;
        # newer sb_secret_ keys go in apikey only.
        if key.startswith("eyJ"):
            headers["Authorization"] = f"Bearer {key}"
        self.client = client or httpx.Client(headers=headers, timeout=60)

    def _object(self, key: str) -> str:
        return f"{quote(self.bucket)}/{quote(key)}"

    def _check(self, response: httpx.Response) -> None:
        if response.status_code >= 400:
            raise UpstreamError(f"Supabase Storage answered {response.status_code}: {response.text[:300]}")

    def put(self, key: str, data: bytes, content_type: str) -> None:
        response = self.client.post(
            f"{self.base}/object/{self._object(key)}",
            content=data,
            headers={"Content-Type": content_type, "x-upsert": "true"},
        )
        self._check(response)

    def get(self, key: str, max_bytes: int) -> bytes | None:
        with self.client.stream("GET", f"{self.base}/object/{self._object(key)}") as response:
            if response.status_code in (400, 404):
                return None
            self._check(response)
            data = bytearray()
            for chunk in response.iter_bytes():
                data += chunk
                if len(data) > max_bytes:
                    raise FileTooLarge(key)
            return bytes(data)

    def delete(self, keys: list[str]) -> None:
        for start in range(0, len(keys), 100):
            response = self.client.request(
                "DELETE", f"{self.base}/object/{quote(self.bucket)}", json={"prefixes": keys[start:start + 100]}
            )
            self._check(response)

    def upload_url(self, key: str) -> str:
        """A one-time URL that accepts a PUT of the file, valid for about two hours."""
        response = self.client.post(f"{self.base}/object/upload/sign/{self._object(key)}")
        self._check(response)
        return self.base + response.json()["url"]

    def download_url(self, key: str, filename: str | None = None) -> str:
        response = self.client.post(f"{self.base}/object/sign/{self._object(key)}", json={"expiresIn": SIGNED_URL_SECONDS})
        self._check(response)
        url = self.base + response.json()["signedURL"]
        if filename:
            url += f"&download={quote(filename)}"
        return url


def open_storage(cfg):
    if cfg.cloud:
        return SupabaseStorage(cfg.supabase_url, cfg.supabase_key, cfg.bucket)
    return LocalStorage(cfg.files_dir)
