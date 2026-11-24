"""CLI entry point: `python -m triposr_lite serve` and `python -m triposr_lite probe`."""

from __future__ import annotations

import argparse
import json
import sys

from . import registry, settings

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def cmd_probe(_args: argparse.Namespace) -> int:
    from .adapter import TripoSRAdapter

    probe = TripoSRAdapter().probe()
    payload = {
        "app": settings.APP_NAME,
        "state": probe.state,
        "reasons": probe.reasons,
        "warnings": probe.warnings,
        "details": probe.details,
        "model": registry.public_card(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    if args.host not in LOOPBACK_HOSTS:
        print(
            f"[拒绝启动] 只允许绑定本机回环地址（127.0.0.1 / localhost），收到 {args.host}。",
            file=sys.stderr,
        )
        return 2

    import uvicorn

    from .app import create_app

    app = create_app(timeout_s=args.timeout)
    print(f"{settings.APP_NAME} v{settings.VERSION} · http://{args.host}:{args.port}")
    print("模型权重不入库；未就绪时界面会显示 NOT_READY 与 DEMO 演示网格。")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="triposr_lite", description=f"{settings.APP_NAME} 本地工作台")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动本地 Web 工作台（仅回环地址）")
    serve.add_argument("--host", default=settings.DEFAULT_HOST)
    serve.add_argument("--port", type=int, default=settings.DEFAULT_PORT)
    serve.add_argument("--timeout", type=int, default=settings.DEFAULT_JOB_TIMEOUT_S, help="单个任务超时秒数")
    serve.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])
    serve.set_defaults(func=cmd_serve)

    probe = sub.add_parser("probe", help="打印模型就绪状态（不下载权重）")
    probe.set_defaults(func=cmd_probe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["serve"] + (argv or []))
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
