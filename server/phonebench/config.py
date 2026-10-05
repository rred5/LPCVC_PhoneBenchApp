"""Server configuration: the repo's config files, a local data folder, and cloud settings.

Local mode keeps everything in the data folder (SQLite, images, results).
Cloud mode is switched on by environment variables:

    PHONEBENCH_DATABASE_URL  Postgres connection string (Supabase pooler)
    SUPABASE_URL             https://<project>.supabase.co
    SUPABASE_SECRET_KEY      the project's secret (service role) key
    PHONEBENCH_BUCKET        storage bucket name (default: phonebench)
"""

from dataclasses import dataclass
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DEFAULT_DATA_DIR = REPO_ROOT / "server" / "data"
DEFAULT_BUCKET = "phonebench"


@dataclass(frozen=True)
class Config:
    data_dir: Path
    defaults: dict
    limits: dict
    queue: dict
    models: tuple[dict, ...]
    min_app_version: str
    database_url: str | None = None
    supabase_url: str | None = None
    supabase_key: str | None = None
    bucket: str = DEFAULT_BUCKET

    @property
    def cloud(self) -> bool:
        """Files and sign-in go through Supabase."""
        return self.supabase_url is not None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "phonebench.sqlite3"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def files_dir(self) -> Path:
        """Local mode's stand-in for the storage bucket."""
        return self.data_dir / "files"

    def model(self, model_id: str) -> dict | None:
        return next((model for model in self.models if model["id"] == model_id), None)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines; blank lines and # comments are skipped."""
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def load_config(
    data_dir: Path | None = None,
    config_dir: Path = CONFIG_DIR,
    limits: dict | None = None,
    queue: dict | None = None,
    env: dict | None = None,
) -> Config:
    """Load config files; `limits` and `queue` override single settings (tests use this).

    `env` replaces the process environment when given.
    """
    env = os.environ if env is None else env
    server = _read_json(config_dir / "server.json")
    data_dir = Path(data_dir or env.get("PHONEBENCH_DATA") or DEFAULT_DATA_DIR)
    supabase_url = (env.get("SUPABASE_URL") or "").rstrip("/") or None
    supabase_key = env.get("SUPABASE_SECRET_KEY") or None
    database_url = env.get("PHONEBENCH_DATABASE_URL") or None
    if supabase_url:
        if not supabase_url.startswith("https://"):
            raise ValueError("SUPABASE_URL must start with https://")
        if not supabase_key or not database_url:
            raise ValueError("Cloud mode needs SUPABASE_SECRET_KEY and PHONEBENCH_DATABASE_URL as well as SUPABASE_URL")
    return Config(
        data_dir=data_dir,
        defaults=_read_json(config_dir / "defaults.json"),
        limits={**server["limits"], **(limits or {})},
        queue={**server["queue"], **(queue or {})},
        models=tuple(_read_json(config_dir / "models.json")),
        min_app_version=server["min_app_version"],
        database_url=database_url,
        supabase_url=supabase_url,
        supabase_key=supabase_key,
        bucket=env.get("PHONEBENCH_BUCKET") or DEFAULT_BUCKET,
    )
