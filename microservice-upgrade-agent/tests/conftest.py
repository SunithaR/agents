"""Test config. Forces LLM_MOCK_MODE and points DATABASE_URL at a temp file
*before* anything under app/ is imported, since app.config.settings is a
module-level singleton built at import time."""
import os
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp()
_db_path = Path(_tmp_dir) / "test.db"

os.environ["LLM_MOCK_MODE"] = "true"
os.environ["DATABASE_URL"] = f"sqlite:///{_db_path}"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import models  # noqa: E402, F401  -- registers tables on Base.metadata
from app.db import Base, engine  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
