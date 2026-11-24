"""Runtime settings for MeshMint-Lite.

All values are read from the environment at call time so tests can override
them without reloading modules. The server must default to loopback only.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "MeshMint-Lite"
VERSION = "0.1.0"

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
WEB_DIR = PACKAGE_DIR / "web"

# Model layout: weights stay outside git; path is configurable.
DEFAULT_MODEL_DIR = REPO_ROOT / "models" / "triposr"
EXPECTED_MODEL_CKPT_BYTES = 1_680_000_000  # HuggingFace file page: 1.68 GB (1.56 GiB)
MIN_MODEL_CKPT_BYTES = 1_000_000_000  # below this a download is almost certainly truncated

MIN_IMAGE_EDGE = 64
MAX_IMAGE_EDGE = 8192
ALLOWED_FORMATS = ("PNG", "JPEG")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 7861
DEFAULT_JOB_TIMEOUT_S = 180


def model_dir() -> Path:
    """Directory that should contain config.yaml and model.ckpt."""
    raw = os.environ.get("MESHMINT_MODEL_DIR", str(DEFAULT_MODEL_DIR))
    return Path(raw).expanduser()


def max_image_bytes() -> int:
    return int(os.environ.get("MESHMINT_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))


def job_timeout_s() -> int:
    return int(os.environ.get("MESHMINT_TIMEOUT_S", str(DEFAULT_JOB_TIMEOUT_S)))


def min_model_ckpt_bytes() -> int:
    return int(os.environ.get("MESHMINT_MIN_CKPT_BYTES", str(MIN_MODEL_CKPT_BYTES)))
