#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 ReSerendipity
# SPDX-License-Identifier: Apache-2.0
"""TripoSR-Lite 产品 CLI：人 / AI agent 可稳定调用的统一入口。

用法：
    triposr probe --json
    triposr inspect -i photo.png --json
    triposr generate -i photo.png --no-remote
    triposr jobs list --json && triposr jobs show <id> --json
    triposr mesh export <id> --format glb -o out.glb --json
    triposr models list --json
    triposr system gpu --json
    triposr serve --port 7861

契约：Result 信封 + Human/JSON 双输出 + 语义退出码；``--json`` 任意位置生效。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# 脚本直跑无包上下文：把仓根注入 sys.path，然后绝对导入。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.integrated_app.cli_output import (  # noqa: E402
    EXIT_OK,
    EXIT_USAGE_ERROR,
    Result,
    begin_json_mode,
    emit,
    emit_progress,
    emit_warning,
    end_json_mode,
)
from app.integrated_app.cli_service import (  # noqa: E402
    CliServiceError,
    batch_generate,
    batch_resume,
    engines_list,
    generate_image,
    inspect_image,
    jobs_cancel,
    jobs_list,
    jobs_show,
    mesh_export,
    mesh_show,
    model_dir,
    model_download,
    model_status,
    model_verify,
    models_list,
    models_show,
    probe_payload,
    serve,
    system_config,
    system_gpu,
    system_health,
)


def _add_common_io(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-i", "--input", required=True, help="输入 PNG/JPEG 图片路径")


def _add_generate_args(parser: argparse.ArgumentParser) -> None:
    _add_common_io(parser)
    parser.add_argument("-o", "--output-dir", default=None, help="产物目录（默认 results/cli/）")
    parser.add_argument("--mc-resolution", type=int, default=256, help="marching cubes 分辨率")
    parser.add_argument("--chunk-size", type=int, default=8192)
    parser.add_argument("--no-remove-bg", action="store_true", help="跳过 rembg 抠图")
    parser.add_argument("--foreground-ratio", type=float, default=0.85)
    parser.add_argument("--timeout", type=int, default=None, help="单任务硬超时秒数")
    parser.add_argument("--server", default=None, help="复用常驻服务 URL（S3）")
    parser.add_argument("--no-remote", action="store_true", help="强制直调，不走 HTTP 服务（S3）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="triposr",
        description="TripoSR-Lite CLI（单图 3D 重建本地工具箱）",
    )
    parser.add_argument("--log-level", default="warning", choices=["critical", "error", "warning", "info", "debug"])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probe", help="模型与环境就绪状态（不下载权重）")

    p_inspect = sub.add_parser("inspect", help="校验图片并给出前景占比提示")
    _add_common_io(p_inspect)
    p_inspect.add_argument("--with-mask", action="store_true", help="输出 base64 前景蒙版预览")

    p_generate = sub.add_parser("generate", help="单图重建 3D 网格")
    _add_generate_args(p_generate)

    p_jobs = sub.add_parser("jobs", help="任务查询与取消")
    jobs_sub = p_jobs.add_subparsers(dest="action", required=True)
    jl = jobs_sub.add_parser("list", help="列出证据目录中的任务")
    jl.add_argument("--limit", type=int, default=20)
    js = jobs_sub.add_parser("show", help="查看单个任务证据")
    js.add_argument("job_id")
    jc = jobs_sub.add_parser("cancel", help="取消仍在运行的任务")
    jc.add_argument("job_id")

    p_mesh = sub.add_parser("mesh", help="网格统计与导出")
    mesh_sub = p_mesh.add_subparsers(dest="action", required=True)
    ms = mesh_sub.add_parser("show", help="查看网格统计")
    ms.add_argument("job_id")
    me = mesh_sub.add_parser("export", help="导出 OBJ / GLB")
    me.add_argument("job_id")
    me.add_argument("--format", default="glb", choices=["obj", "glb"])
    me.add_argument("-o", "--output", default=None)

    p_export = sub.add_parser("export", help="mesh export 的简写")
    p_export.add_argument("job_id")
    p_export.add_argument("--format", default="glb", choices=["obj", "glb"])
    p_export.add_argument("-o", "--output", default=None)

    p_models = sub.add_parser("models", help="模型清单")
    models_sub = p_models.add_subparsers(dest="action", required=True)
    models_sub.add_parser("list", help="列出权重文件与完整性")
    mshow = models_sub.add_parser("show", help="查看模型卡")
    mshow.add_argument("model_id", nargs="?", default="triposr")

    p_engines = sub.add_parser("engines", help="引擎清单")
    p_engines.add_subparsers(dest="action", required=True).add_parser("list")

    p_model = sub.add_parser("model", help="模型运维")
    model_sub = p_model.add_subparsers(dest="action", required=True)
    model_sub.add_parser("status", help="就绪状态与修复指引")
    model_sub.add_parser("verify", help="核对权重字节数")
    model_sub.add_parser("dir", help="打印模型目录")
    mdl = model_sub.add_parser("download", help="打印或执行官方下载命令")
    mdl.add_argument("--execute", action="store_true", help="真正执行下载（默认只打印）")

    p_download = sub.add_parser("download", help="model download 的简写")
    p_download.add_argument("--execute", action="store_true")

    p_batch = sub.add_parser("batch", help="批量生成")
    batch_sub = p_batch.add_subparsers(dest="action", required=True)
    bg = batch_sub.add_parser("generate", help="逐张生成，失败项写批次摘要")
    bg.add_argument("-i", "--input", nargs="+", required=True, help="一个或多个图片路径")
    bg.add_argument("-o", "--output-dir", default=None)
    bg.add_argument("--mc-resolution", type=int, default=256)
    bg.add_argument("--chunk-size", type=int, default=8192)
    bg.add_argument("--timeout", type=int, default=None)
    br = batch_sub.add_parser("resume", help="重试批次中的失败项")
    br.add_argument("batch_id")
    br.add_argument("--timeout", type=int, default=None)

    p_serve = sub.add_parser("serve", help="启动本地 Web 工作台（仅回环地址）")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=7861)
    p_serve.add_argument("--timeout", type=int, default=180)
    p_serve.add_argument("--log-level", default="info", choices=["critical", "error", "warning", "info", "debug"])

    p_system = sub.add_parser("system", help="环境状态")
    system_sub = p_system.add_subparsers(dest="action", required=True)
    system_sub.add_parser("health", help="服务与目录状态")
    system_sub.add_parser("gpu", help="CUDA / 显卡状态")
    system_sub.add_parser("config", help="脱敏后的关键配置")

    return parser


def _human(result: Result) -> None:
    if result.ok:
        print(json.dumps(result.data, ensure_ascii=False, indent=2))
    else:
        print(f"错误: {result.error}", file=sys.stderr)
        if result.error_code:
            print(f"错误码: {result.error_code}", file=sys.stderr)
        if result.hint:
            print(f"修复建议: {result.hint}", file=sys.stderr)


def _ok(data: dict, command: str) -> Result:
    return Result.success(data, command=command)


def _failure(exc: Exception, command: str) -> Result:
    if isinstance(exc, CliServiceError):
        return Result.failure(
            exc.message,
            exc.code,
            hint=exc.hint,
            details=exc.details,
            command=command,
        )
    if isinstance(exc, FileNotFoundError):
        return Result.failure(str(exc), "USAGE_ERROR", command=command)
    if isinstance(exc, ValueError):
        return Result.failure(str(exc), "USAGE_ERROR", command=command)
    return Result.failure(f"{type(exc).__name__}: {exc}", "BUSINESS_ERROR", command=command)


def _dispatch(args: argparse.Namespace, as_json: bool, command: str) -> int:
    action = getattr(args, "action", "")

    def progress(stage: str, ratio: float) -> None:
        emit_progress(stage, ratio, as_json)

    if command == "probe":
        return emit(_ok(probe_payload(), command), as_json, _human)
    if command == "inspect":
        return emit(_ok(inspect_image(args.input, with_mask=args.with_mask), command), as_json, _human)
    if command == "generate":
        data = generate_image(
            args.input,
            output_dir=args.output_dir,
            mc_resolution=args.mc_resolution,
            chunk_size=args.chunk_size,
            remove_background=not args.no_remove_bg,
            foreground_ratio=args.foreground_ratio,
            timeout_s=args.timeout,
            progress_cb=progress,
        )
        return emit(_ok(data, command), as_json, _human)
    if command == "jobs" and action == "list":
        return emit(_ok(jobs_list(limit=args.limit), command), as_json, _human)
    if command == "jobs" and action == "show":
        return emit(_ok(jobs_show(args.job_id), command), as_json, _human)
    if command == "jobs" and action == "cancel":
        return emit(_ok(jobs_cancel(args.job_id), command), as_json, _human)
    if command == "mesh" and action == "show":
        return emit(_ok(mesh_show(args.job_id), command), as_json, _human)
    if (command == "mesh" and action == "export") or command == "export":
        data = mesh_export(args.job_id, args.format, args.output)
        return emit(_ok(data, command), as_json, _human)
    if command == "models" and action == "list":
        return emit(_ok(models_list(), command), as_json, _human)
    if command == "models" and action == "show":
        return emit(_ok(models_show(args.model_id), command), as_json, _human)
    if command == "engines" and action == "list":
        return emit(_ok(engines_list(), command), as_json, _human)
    if command == "model" and action == "status":
        return emit(_ok(model_status(), command), as_json, _human)
    if command == "model" and action == "verify":
        return emit(_ok(model_verify(), command), as_json, _human)
    if command == "model" and action == "dir":
        return emit(_ok(model_dir(), command), as_json, _human)
    if (command == "model" and action == "download") or command == "download":
        return emit(_ok(model_download(execute=args.execute), command), as_json, _human)
    if command == "batch" and action == "generate":
        data = batch_generate(
            args.input,
            output_dir=args.output_dir,
            mc_resolution=args.mc_resolution,
            chunk_size=args.chunk_size,
            timeout_s=args.timeout,
            progress_cb=progress,
        )
        return emit(_ok(data, command), as_json, _human)
    if command == "batch" and action == "resume":
        return emit(_ok(batch_resume(args.batch_id, timeout_s=args.timeout), command), as_json, _human)
    if command == "serve":
        data = serve(
            host=args.host,
            port=args.port,
            timeout_s=args.timeout,
            log_level=args.log_level,
        )
        return emit(_ok(data, command), as_json, _human)
    if command == "system" and action == "health":
        return emit(_ok(system_health(), command), as_json, _human)
    if command == "system" and action == "gpu":
        return emit(_ok(system_gpu(), command), as_json, _human)
    if command == "system" and action == "config":
        return emit(_ok(system_config(), command), as_json, _human)

    emit_warning(f"未知命令组合: {command} {action}")
    return EXIT_USAGE_ERROR


def main(argv: list[str] | None = None) -> int:
    """入口：管理 JSON 模式的 stdout 隔离生命周期。"""
    raw = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in raw
    if as_json:
        raw = [arg for arg in raw if arg != "--json"]
        begin_json_mode()
    try:
        return _main_impl(raw, as_json)
    finally:
        if as_json:
            end_json_mode()


def _main_impl(raw: list[str], as_json: bool) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(raw)
    except SystemExit as exc:
        if exc.code == 0:
            return EXIT_OK
        # argparse 的错误也必须落在 Result 信封里，AI agent 才能统一解析。
        command = raw[0] if raw else ""
        return emit(
            Result.failure("参数错误（见 stderr 的 argparse 提示）", "USAGE_ERROR", command=command),
            as_json,
            _human,
        )

    command = args.command
    try:
        return _dispatch(args, as_json, command)
    except Exception as exc:  # noqa: BLE001 - 统一收口，绝不把 traceback 当结果
        return emit(_failure(exc, command), as_json, _human)


if __name__ == "__main__":
    sys.exit(main())
