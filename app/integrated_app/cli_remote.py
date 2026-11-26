#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 ReSerendipity
# SPDX-License-Identifier: Apache-2.0
"""CLI 远程层：可选复用已运行的 FastAPI 常驻服务。

实测契约（2026-11-26，triposr_lite/app.py）：
- ``GET /api/health`` → ``{"status":"ok","app":...,"version":...}``；
- ``POST /api/inspect`` (multipart file) → ``{"image":...,"foreground":...}``；
- ``POST /api/jobs`` (multipart file) → ``{"job":{...},"mesh":...,"probe":...}``；
- ``GET /api/jobs/{id}`` → 同上，job.terminal 可判终态；
- ``GET /api/jobs/{id}/mesh`` → ``{"mesh":{...,"stats":...}}``；
- ``GET /api/jobs/{id}/export?format=obj|glb`` → 文件流（不是路径）。
无鉴权、无 CSRF 握手。

**收益边界（ADR 方案 B）**：模型由监督脚本子进程每任务加载，HTTP 只省服务启动，
不省 1.68GB 权重加载。所有失败都抛 ``RemoteError``，由 CLI 回落直调，行为不退化。
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx

from triposr_lite.server_port import read_port_file

ProgressCb = Callable[[str, float], None]
ENV_SERVER = "TRIPOSR_CLI_SERVER"


class RemoteError(RuntimeError):
    """服务不可用或返回不符合契约；调用方必须回落直调。"""


def _client(base_url: str, timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(
        base_url=base_url.rstrip("/"),
        timeout=timeout,
        trust_env=True,
        transport=transport,
    )


def probe(base_url: str, timeout: float = 2.0, transport: httpx.BaseTransport | None = None) -> dict:
    """短超时探测：必须是本项目的健康服务。"""
    try:
        with _client(base_url, timeout, transport) as client:
            resp = client.get("/api/health")
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001 - 定义为「探测失败即降级」
        raise RemoteError(f"无法连接常驻服务 {base_url}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise RemoteError(f"{base_url} 不是健康服务：{payload}")
    if "TripoSR" not in str(payload.get("app", "")):
        raise RemoteError(f"{base_url} 不是 TripoSR-Lite 服务：{payload.get('app')}")
    return payload


def discover_server(
    explicit: str | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
) -> str | None:
    """服务发现：显式 URL > 环境变量 > 端口文件；2 秒短超时。"""
    candidates: list[str] = []
    for candidate in (explicit, os.environ.get(ENV_SERVER, "").strip()):
        if candidate:
            candidates.append(candidate.rstrip("/"))
    port = read_port_file()
    if port:
        candidates.append(f"http://127.0.0.1:{port}")
    for base in candidates:
        try:
            probe(base, transport=transport)
        except RemoteError:
            continue
        return base
    return None


def _csrf_headers(client: httpx.Client) -> dict[str, str]:
    """自适应 double-submit（本仓当前关闭；保留以免未来加中间件时静默失败）。"""
    try:
        resp = client.get("/api/health")
        token = resp.headers.get("X-CSRF-Token")
        cookie = resp.headers.get("Set-Cookie", "")
        if not token and "csrf_token=" in cookie:
            token = cookie.split("csrf_token=", 1)[1].split(";", 1)[0]
        if token:
            return {"X-CSRF-Token": token, "Cookie": f"csrf_token={token}"}
    except Exception:  # noqa: BLE001 - 无 CSRF 时返回空头
        pass
    return {}


def inspect(base_url: str, path: str, *, with_mask: bool = False) -> dict:
    source = Path(path).expanduser()
    if not source.is_file():
        raise RemoteError(f"输入文件不存在：{source}")
    try:
        with _client(base_url) as client:
            headers = _csrf_headers(client)
            with source.open("rb") as fh:
                resp = client.post(
                    "/api/inspect",
                    files={"file": (source.name, fh, "image/png")},
                    headers=headers,
                )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        raise RemoteError(f"/api/inspect 调用失败：{exc}") from exc
    if not isinstance(payload, dict) or "image" not in payload:
        raise RemoteError(f"/api/inspect 返回不符合契约：{str(payload)[:200]}")
    result = {"path": str(source.resolve()), "image": payload["image"], "foreground": payload.get("foreground", {})}
    if not with_mask:
        result["foreground"].pop("mask_data_url", None)
    return result


def _download(client: httpx.Client, url: str, target: Path) -> int:
    """流式落盘：网格是大文件，绝不整块读进内存。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with target.open("wb") as fh:
                for chunk in resp.iter_bytes(8192):
                    fh.write(chunk)
                    written += len(chunk)
    except Exception as exc:  # noqa: BLE001
        raise RemoteError(f"下载产物失败 {url}: {exc}") from exc
    if written == 0:
        raise RemoteError(f"下载产物为空：{url}")
    return written


def generate(
    base_url: str,
    path: str,
    *,
    output_dir: str | None = None,
    mc_resolution: int = 256,
    chunk_size: int = 8192,
    remove_background: bool = True,
    foreground_ratio: float = 0.85,
    timeout_s: int | None = None,
    progress_cb: ProgressCb | None = None,
) -> dict:
    """走 HTTP 提交任务并轮询终态，产物流式落盘，返回与直调兼容的结构。"""
    # 远程提交端点当前不接受生成参数；发现非默认值就如实拒绝并回落直调。
    defaults = (
        (mc_resolution, 256, "--mc-resolution"),
        (chunk_size, 8192, "--chunk-size"),
        (foreground_ratio, 0.85, "--foreground-ratio"),
    )
    for value, default, flag in defaults:
        if abs(float(value) - float(default)) > 1e-9:
            raise RemoteError(f"常驻服务不支持 {flag} 非默认值，回落直调。")
    if not remove_background:
        raise RemoteError("常驻服务不支持跳过抠图，回落直调。")

    source = Path(path).expanduser()
    if not source.is_file():
        raise RemoteError(f"输入文件不存在：{source}")
    out_dir = (Path(output_dir).expanduser() if output_dir else Path.cwd() / "results" / "cli")

    with _client(base_url) as client:
        headers = _csrf_headers(client)
        try:
            with source.open("rb") as fh:
                resp = client.post(
                    "/api/jobs",
                    files={"file": (source.name, fh, "image/png")},
                    headers=headers,
                )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise RemoteError(f"/api/jobs 提交失败：{exc}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("job"), dict):
            raise RemoteError(f"/api/jobs 返回不符合契约：{str(payload)[:200]}")

        job = payload["job"]
        job_id = str(job.get("id", ""))
        if not job_id:
            raise RemoteError("服务未返回 job.id")

        deadline = time.monotonic() + (timeout_s or 3600)
        last_stage = ""
        while True:
            if time.monotonic() > deadline:
                raise RemoteError(f"任务 {job_id} 轮询超时")
            try:
                status_resp = client.get(f"/api/jobs/{job_id}")
                status_resp.raise_for_status()
                status_payload = status_resp.json()
            except Exception as exc:  # noqa: BLE001
                raise RemoteError(f"轮询任务失败：{exc}") from exc
            job = status_payload.get("job", {}) if isinstance(status_payload, dict) else {}
            if not job:
                raise RemoteError("轮询响应缺 job 字段")
            stage = str(job.get("stage", ""))
            if stage != last_stage:
                last_stage = stage
                if progress_cb:
                    progress_cb(stage, float(job.get("progress", 0.0)) or 0.0)
            if job.get("terminal"):
                break
            time.sleep(0.5)

        if job.get("status") != "DONE":
            raise RemoteError(f"任务终态 {job.get('status')}: {job.get('error_code') or job.get('message', '')}")

        target_dir = out_dir / job_id
        paths: dict[str, str] = {}
        for fmt in ("obj", "glb"):
            target = target_dir / f"mesh.{fmt}"
            _download(client, f"/api/jobs/{job_id}/export?format={fmt}", target)
            paths[fmt] = str(target.resolve())

        stats: dict[str, Any] = {}
        try:
            mesh_resp = client.get(f"/api/jobs/{job_id}/mesh")
            mesh_resp.raise_for_status()
            stats = (mesh_resp.json().get("mesh") or {}).get("stats", {})
        except Exception:  # noqa: BLE001 - stats 缺失不影响产物
            stats = {}

    return {
        "job_id": job_id,
        "input": str(source.resolve()),
        "output_dir": str(target_dir.resolve()),
        "obj": paths["obj"],
        "glb": paths["glb"],
        "device": (job.get("timings") or {}).get("device", "remote"),
        "stats": stats,
        "mode": "remote",
        "server_url": base_url.rstrip("/"),
    }


def iter_error_cases() -> Iterator[str]:
    """枚举必须回落直调的降级路径（供契约测试与文档引用）。"""
    yield "服务不可达"
    yield "服务版本不符"
    yield "端点缺失或返回非契约 JSON"
    yield "任务终态非 DONE（NOT_READY/FAILED/TIMEOUT）"
    yield "轮询或下载超时"
    yield "产物流式下载失败或为空"


__all__ = ["ENV_SERVER", "RemoteError", "discover_server", "generate", "inspect", "iter_error_cases", "probe"]
