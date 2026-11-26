"""桌面壳动态端口的落地文件：供 CLI 服务发现读取。

写侧：`scripts/desktop_launch.py`（壳分配端口后立刻写）。
读侧：`app/integrated_app/cli_remote.py`（服务发现最后一级）。

端口文件内容只有一个十进制整数；安装态放 `%LOCALAPPDATA%\\TripoSR-Lite\\.server_port`，
源码态放仓根 `app/.server_port`。两条路径都不入库。
"""
from __future__ import annotations

import os
from pathlib import Path

ENV_PORT_FILE = "TRIPOSR_PORT_FILE"
_APP_DIR = Path(__file__).resolve().parent.parent


def port_file_path() -> Path:
    override = os.environ.get(ENV_PORT_FILE, "").strip()
    if override:
        return Path(override).expanduser()
    # 安装态判定与 desktop_launch.py 的 _packaged_defaults 一致。
    if (_APP_DIR / "runtime" / "python.exe").is_file():
        local = Path(os.environ.get("LOCALAPPDATA", str(_APP_DIR / ".local"))) / "TripoSR-Lite"
        return local / ".server_port"
    return _APP_DIR / "app" / ".server_port"


def write_port_file(port: int) -> Path:
    path = port_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(int(port)), encoding="ascii")
    return path


def read_port_file() -> int | None:
    path = port_file_path()
    try:
        value = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    return value if 1 < value < 65536 else None


def clear_port_file() -> None:
    try:
        port_file_path().unlink()
    except OSError:
        pass
