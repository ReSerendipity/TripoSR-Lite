"""Shared fixtures: synthetic images and a test app with no model weights."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import triposr_lite.adapter as adapter_mod  # noqa: E402
from triposr_lite.adapter import TripoSRAdapter, _reset_runtime_cache  # noqa: E402
from triposr_lite.app import create_app  # noqa: E402

from helpers import png_bytes  # noqa: E402

ENV_KEYS = (
    "TRIPOSR_MODEL_DIR",
    "TRIPOSR_MAX_IMAGE_BYTES",
    "TRIPOSR_MIN_CKPT_BYTES",
    "TRIPOSR_UPSTREAM_DIR",
    "TRIPOSR_RUNTIME_PYTHON",
    "TRIPOSR_SUPERVISOR",
    "TRIPOSR_RESULTS_DIR",
    "TRIPOSR_MIN_FREE_MIB",
    "TRIPOSR_DEVICE",
    "TRIPOSR_PROBE_TIMEOUT_S",
    "TRIPOSR_PROBE_TTL_S",
)


@pytest.fixture(autouse=True)
def _clean_runtime(monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    # Tests never spawn the real external interpreter; probe tests opt in explicitly.
    monkeypatch.setattr(
        adapter_mod,
        "_external_runtime_probe",
        lambda *args, **kwargs: {
            "ok": False,
            "code": "MISSING_TORCH",
            "error": "test stub: 外部运行时未探测",
            "python": str(args[0]) if args else None,
            "upstream_dir": str(args[1]) if len(args) > 1 else None,
        },
    )
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
