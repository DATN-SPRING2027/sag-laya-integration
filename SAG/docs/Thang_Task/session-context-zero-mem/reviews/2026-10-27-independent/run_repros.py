"""Run offline reproductions; PG cases require our separate disposable QA container."""

import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

scratch = Path(__file__).resolve().parent
root = next(p for p in scratch.parents if (p / "SAG/apps/api").is_dir())
api = root / "SAG/apps/api"
os.chdir(api)
sys.path[:0] = [str(api), str(api / "tests")]
os.environ["SAG_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.environ["SESSION_CONTEXT_NATIVE_PG_QA"] = "1" if "--native-pg" in sys.argv else "0"
with patch("pydantic_settings.sources.providers.dotenv.DotEnvSettingsSource._read_env_files", return_value={}):
    raise SystemExit(
        pytest.main(["-c", "pyproject.toml", "-q", "-s", "--tb=short", str(scratch / "reproduction_cases.py")])
    )
