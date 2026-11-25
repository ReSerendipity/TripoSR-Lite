"""Runtime settings for TripoSR-Lite.

All values are read from the environment at call time so tests can override
them without reloading modules. The server must default to loopback only.
"""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "TripoSR-Lite"
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

# External TripoSR runtime: the app process never imports torch itself.
DEFAULT_UPSTREAM_DIR = REPO_ROOT / "external" / "TripoSR"
DEFAULT_RUNTIME_PYTHON = DEFAULT_UPSTREAM_DIR / ".venv" / "Scripts" / "python.exe"
DEFAULT_SUPERVISOR = REPO_ROOT / "scripts" / "run_triposr_real.py"
COMPAT_DIR = REPO_ROOT / "scripts" / "_compat"
DEFAULT_MIN_FREE_MIB = 7168
DEFAULT_RESULTS_DIR = REPO_ROOT / "results"


def _path_env(name: str, default: Path) -> Path:
    return Path(os.environ.get(name, str(default))).expanduser()


def model_dir() -> Path:
    """Directory that should contain config.yaml and model.ckpt."""
    return _path_env("TRIPOSR_MODEL_DIR", DEFAULT_MODEL_DIR)


def upstream_dir() -> Path:
    return _path_env("TRIPOSR_UPSTREAM_DIR", DEFAULT_UPSTREAM_DIR)


def runtime_python() -> Path:
    return _path_env("TRIPOSR_RUNTIME_PYTHON", DEFAULT_RUNTIME_PYTHON)


def supervisor_script() -> Path:
    return _path_env("TRIPOSR_SUPERVISOR", DEFAULT_SUPERVISOR)


def results_dir() -> Path:
    return _path_env("TRIPOSR_RESULTS_DIR", DEFAULT_RESULTS_DIR)


def min_free_mib() -> int:
    return int(os.environ.get("TRIPOSR_MIN_FREE_MIB", str(DEFAULT_MIN_FREE_MIB)))


def runtime_probe_timeout_s() -> float:
    return float(os.environ.get("TRIPOSR_PROBE_TIMEOUT_S", "30"))


def runtime_probe_ttl_s() -> float:
    return float(os.environ.get("TRIPOSR_PROBE_TTL_S", "30"))


def max_image_bytes() -> int:
    return int(os.environ.get("TRIPOSR_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))


def job_timeout_s() -> int:
    return int(os.environ.get("TRIPOSR_TIMEOUT_S", str(DEFAULT_JOB_TIMEOUT_S)))


def min_model_ckpt_bytes() -> int:
    return int(os.environ.get("TRIPOSR_MIN_CKPT_BYTES", str(MIN_MODEL_CKPT_BYTES)))
