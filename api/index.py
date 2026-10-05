"""Vercel entry point: the whole PhoneBench API as one Python function.

vercel.json sends every /api/... request here; FastAPI routes it by the
original path. The settings come from the Vercel project's environment
variables (see docs/USAGE.md, "Deploying to Vercel").
"""

import logging
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from phonebench.api import create_app  # noqa: E402
from phonebench.config import load_config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

cfg = load_config()
if not cfg.cloud:
    # Without Supabase the server would try to write a local database, which a
    # Vercel function cannot keep.
    raise RuntimeError(
        "Set SUPABASE_URL, SUPABASE_SECRET_KEY, SUPABASE_PUBLISHABLE_KEY and "
        "PHONEBENCH_DATABASE_URL in the Vercel project's environment variables"
    )
app = create_app(cfg)
