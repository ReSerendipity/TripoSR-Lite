"""Shared fixtures: synthetic images and a test app with no model weights."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from meshmint.adapter import TripoSRAdapter, _reset_runtime_cache  # noqa: E402
from meshmint.app import create_app  # noqa: E402

from helpers import png_bytes  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    monkeypatch.delenv("MESHMINT_MODEL_DIR", raising=False)
    monkeypatch.delenv("MESHMINT_MAX_IMAGE_BYTES", raising=False)
    monkeypatch.delenv("MESHMINT_MIN_CKPT_BYTES", raising=False)
    _reset_runtime_cache()
    yield
    _reset_runtime_cache()


@pytest.fixture
def png() -> bytes:
    return png_bytes()


@pytest.fixture
def client(tmp_path) -> TestClient:
    adapter = TripoSRAdapter(weights_dir=tmp_path / "models" / "triposr")
    app = create_app(adapter=adapter, timeout_s=10)
    with TestClient(app) as test_client:
        yield test_client
