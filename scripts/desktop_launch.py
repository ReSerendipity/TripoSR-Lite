"""桌面壳启动入口：接收 --host/--port，启动现有 FastAPI 服务。

与 Tauri 壳的双向契约：
- `--host` / `--port` 必填（端口由壳先绑定 127.0.0.1:0 抓空闲端口后传入，脚本不再探测）。
- 开头打印一行 `监听端口: N`，作为装后动态端口的唯一日志锚点。
- 不调用 `webbrowser.open`（WebView 即浏览器），也不做 pip 安装（runtime 已随包）。
- stdout/stderr 由壳重定向到 `logs/desktop_python_*.log`，统一 UTF-8。

安装态（`<app>/runtime/python.exe` 存在）缺省把模型/结果/外部运行时指向包内或
`%LOCALAPPDATA%\\TripoSR-Lite`；显式环境变量（壳注入）优先，绝不被脚本覆盖。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _reconfigure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _packaged_defaults(app_root: Path) -> None:
    """只在安装态补缺省；已有的环境变量一律优先（壳注入 TRIPOSR_* 覆盖）。"""
    if not (app_root / "runtime" / "python.exe").is_file():
        return
    local = Path(os.environ.get("LOCALAPPDATA", str(app_root / ".local"))) / "TripoSR-Lite"
    external = app_root / "external" / "TripoSR"
    runtime = external / "runtime" / "python.exe"
    defaults = {
        "TRIPOSR_MODEL_DIR": local / "models" / "triposr",
        "TRIPOSR_RESULTS_DIR": local / "results",
        "TRIPOSR_UPSTREAM_DIR": external,
        "TRIPOSR_SUPERVISOR": app_root / "scripts" / "run_triposr_real.py",
        # 首次冷启动要加载 4GB+ torch/CUDA DLL（杀软首扫更久），实测 130–172s，30s 会误报探测失败。
        "TRIPOSR_PROBE_TIMEOUT_S": "300",
        "TRIPOSR_PROBE_TTL_S": "300",
        # pymatting/rembg 的 numba 缓存默认写在源码旁；安装目录应保持干净，统一挪到用户数据目录。
        "NUMBA_CACHE_DIR": str(local / "cache" / "numba"),
    }
    if runtime.is_file():
        defaults["TRIPOSR_RUNTIME_PYTHON"] = runtime
    for name, value in defaults.items():
        if not os.environ.get(name):
            os.environ[name] = str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TripoSR-Lite 桌面壳后端启动器")
    parser.add_argument("--host", required=True, help="监听地址（壳固定传 127.0.0.1）")
    parser.add_argument("--port", required=True, type=int, help="监听端口（由壳预分配）")
    args = parser.parse_args(argv)

    _reconfigure_stdout()
    app_root = Path(__file__).resolve().parent.parent
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))

    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    _packaged_defaults(app_root)

    # 先打印端口锚点：即使后续导入/加载失败，壳也能在日志里定位本次端口。
    print(f"监听端口: {args.port}", flush=True)
    # 端口文件：CLI 服务发现的最后一级；写入失败不阻断服务启动。
    clear_port_file = None
    try:
        from triposr_lite.server_port import clear_port_file, write_port_file

        write_port_file(args.port)
    except Exception as exc:  # noqa: BLE001 - 端口文件可选
        print(f"[TripoSR-Lite] 端口文件写入失败（CLI 会降级直调）：{exc}", file=sys.stderr, flush=True)
    print(f"[TripoSR-Lite] 应用目录：{app_root}", flush=True)
    print(
        "[TripoSR-Lite] 模型目录："
        + os.environ.get("TRIPOSR_MODEL_DIR", str(app_root / "models" / "triposr")),
        flush=True,
    )
    print(
        "[TripoSR-Lite] 外部运行时："
        + os.environ.get("TRIPOSR_RUNTIME_PYTHON", "(默认 external/TripoSR/.venv)"),
        flush=True,
    )

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"[TripoSR-Lite] 警告：正在监听 {args.host}，服务将对网络可达；桌面壳只会传 127.0.0.1。",
            file=sys.stderr,
            flush=True,
        )

    try:
        import uvicorn

        from triposr_lite.app import app
    except Exception as exc:  # noqa: BLE001 - 打包 runtime 缺依赖时必须明确失败
        print(f"[TripoSR-Lite] 启动失败：导入依赖异常（{exc}）", file=sys.stderr, flush=True)
        return 2

    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    finally:
        if clear_port_file is not None:
            clear_port_file()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
