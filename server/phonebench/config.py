"""Server configuration: the repo's config files plus a local data folder."""

from dataclasses import dataclass
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_DATA_DIR = REPO_ROOT / "server" / "data"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    defaults: dict
    limits: dict
    queue: dict
    models: tuple[dict, ...]
    min_app_version: str

    @property
    def db_path(self) -> Path:
        return self.data_dir / "phonebench.sqlite3"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def device_key_path(self) -> Path:
        return self.data_dir / "device_key.txt"

    def model(self, model_id: str) -> dict | None:
        return next((model for model in self.models if model["id"] == model_id), None)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_config(
    data_dir: Path | None = None,
    config_dir: Path = CONFIG_DIR,
    limits: dict | None = None,
    queue: dict | None = None,
) -> Config:
    """Load config files; `limits` and `queue` override single settings (tests use this)."""
    server = _read_json(config_dir / "server.json")
    data_dir = Path(data_dir or os.environ.get("PHONEBENCH_DATA") or DEFAULT_DATA_DIR)
    return Config(
        data_dir=data_dir,
        defaults=_read_json(config_dir / "defaults.json"),
        limits={**server["limits"], **(limits or {})},
        queue={**server["queue"], **(queue or {})},
        models=tuple(_read_json(config_dir / "models.json")),
        min_app_version=server["min_app_version"],
    )
