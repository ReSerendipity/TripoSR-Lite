#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 ReSerendipity
# SPDX-License-Identifier: Apache-2.0
"""CLI 统一输出层：Result 信封、Human/JSON 双渲染、语义退出码。

契约（见 docs/ADR-CLI-AND-UPDATE.md）：
- stdout 只承载结果数据；进度/警告/日志一律 stderr；
- ``--json`` 在 cli.main() 里预提取，任意位置生效；
- 退出码 0 成功 / 1 运行时 / 2 用法 / 3 未就绪。

本模块不依赖 FastAPI / torch，可在纯 CLI 环境直接 import。
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

EXIT_OK = 0
EXIT_BUSINESS_ERROR = 1
EXIT_USAGE_ERROR = 2
EXIT_NOT_READY = 3

# 未就绪类：环境前置条件不满足，用户需要装权重/腾显存/等 GPU，而不是改代码。
NOT_READY_CODES = frozenset(
    {
        "NOT_READY",
        "MISSING_CONFIG",
        "MISSING_WEIGHTS",
        "WEIGHT_TOO_SMALL",
        "MISSING_TORCH",
        "MISSING_UPSTREAM_CODE",
        "RUNTIME_PROBE_FAILED",
        "GPU_BUSY",
        "INSUFFICIENT_VRAM",
    }
)

# 用法类：调用方给错参数/输入文件/任务 id。
USAGE_CODES = frozenset(
    {
        "USAGE_ERROR",
        "INVALID_FORMAT",
        "JOB_NOT_FOUND",
        "IMAGE_EMPTY",
        "IMAGE_UNREADABLE",
        "IMAGE_TOO_LARGE",
        "IMAGE_TOO_SMALL",
        "IMAGE_TOO_LARGE_DIMENSIONS",
        "UNSUPPORTED_FORMAT",
    }
)


@dataclass
class Result:
    """命令执行结果的统一信封。"""

    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    error_code: str = "BUSINESS_ERROR"
    hint: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    command: str = ""

    @classmethod
    def success(cls, data: dict[str, Any] | None = None, **kwargs: Any) -> "Result":
        return cls(ok=True, data=dict(data or {}), **kwargs)

    @classmethod
    def failure(
        cls,
        error: str,
        error_code: str = "BUSINESS_ERROR",
        **kwargs: Any,
    ) -> "Result":
        return cls(ok=False, error=error, error_code=error_code, **kwargs)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": self.ok, "command": self.command}
        if self.ok:
            payload["data"] = self.data
        else:
            payload["error"] = self.error
            payload["error_code"] = self.error_code
        if self.hint:
            payload["hint"] = self.hint
        if self.details:
            payload["details"] = self.details
        if self.warnings:
            payload["warnings"] = self.warnings
        return payload

    def exit_code(self) -> int:
        if self.ok:
            return EXIT_OK
        if self.error_code in USAGE_CODES:
            return EXIT_USAGE_ERROR
        if self.error_code in NOT_READY_CODES:
            return EXIT_NOT_READY
        return EXIT_BUSINESS_ERROR


_ORIGINAL_STDOUT: Any = None


def begin_json_mode() -> Any:
    """把 import/引擎期的 stdout 污染重定向到 stderr，返回原始 stdout。

    必须在导入有 print 副作用的依赖之前调用。
    """
    global _ORIGINAL_STDOUT
    if _ORIGINAL_STDOUT is None:
        _ORIGINAL_STDOUT = sys.stdout
        sys.stdout = sys.stderr
    return _ORIGINAL_STDOUT


def end_json_mode() -> None:
    """恢复 stdout（供进程内多次调用/测试使用）。"""
    global _ORIGINAL_STDOUT
    if _ORIGINAL_STDOUT is not None:
        sys.stdout = _ORIGINAL_STDOUT
        _ORIGINAL_STDOUT = None


def _result_stream() -> Any:
    return _ORIGINAL_STDOUT if _ORIGINAL_STDOUT is not None else sys.stdout


def emit(result: Result, as_json: bool, human_formatter: Callable[[Result], None] | None = None) -> int:
    """渲染结果并返回进程退出码。"""
    if as_json:
        payload = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=False)
        print(payload, file=_result_stream())
    elif human_formatter is not None:
        human_formatter(result)
    elif result.ok:
        for key, value in result.data.items():
            print(f"{key}: {value}")
    else:
        print(f"错误: {result.error}", file=sys.stderr)
        if result.error_code:
            print(f"错误码: {result.error_code}", file=sys.stderr)
        if result.hint:
            print(f"修复建议: {result.hint}", file=sys.stderr)
    return result.exit_code()


def emit_warning(message: str) -> None:
    print(f"警告: {message}", file=sys.stderr)


def emit_progress(stage: str, ratio: float, as_json: bool) -> None:
    """推理阶段进度：JSON 模式也必须走 stderr，绝不污染 stdout。"""
    if as_json:
        print(f"[{stage}] {ratio * 100:.0f}%", file=sys.stderr)
    else:
        print(f"[{stage}] {ratio * 100:.0f}%", file=sys.stderr)


__all__ = [
    "EXIT_OK",
    "EXIT_BUSINESS_ERROR",
    "EXIT_USAGE_ERROR",
    "EXIT_NOT_READY",
    "NOT_READY_CODES",
    "USAGE_CODES",
    "Result",
    "begin_json_mode",
    "end_json_mode",
    "emit",
    "emit_progress",
    "emit_warning",
]
