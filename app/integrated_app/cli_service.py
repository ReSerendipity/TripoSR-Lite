#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 ReSerendipity
# SPDX-License-Identifier: Apache-2.0
"""CLI 服务层：把 Web/适配器能力收敛成同步、可 JSON 化的函数。

纪律：
- 每个命令独立进程，模型 load 不跨命令保持（S3 的 HTTP 复用只省服务启动）；
- 所有失败都带稳定 error_code + hint，绝不静默成功；
- 产物落固定 results/cli/<job_id>/，与 Web 的 results/app-* 证据目录并存。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable, cast

import trimesh

from triposr_lite import registry, settings
from triposr_lite.adapter import (
    AdapterError,
    InferenceResult,
    TripoSRAdapter,
)
from triposr_lite.imaging import ImageValidationError, estimate_foreground, validate_image_bytes
from triposr_lite.meshes import (
    MeshRecord,
    export_glb,
    export_obj,
    mesh_stats,
)
from triposr_lite.settings import APP_NAME, VERSION

ProgressCb = Callable[[str, float], None]

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


class CliServiceError(Exception):
    """带稳定错误码与修复指引的服务层异常。"""

    def __init__(self, code: str, message: str, hint: str = "", details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint
        self.details = details or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "hint": self.hint, "details": self.details}


# ------------------------------------------------------------------ helpers
def adapter() -> TripoSRAdapter:
    return TripoSRAdapter()


def cli_results_dir() -> Path:
    root = os.environ.get("TRIPOSR_CLI_RESULTS_DIR", "").strip()
    return Path(root).expanduser() if root else settings.results_dir() / "cli"


def _read_json(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _tail(path: Path, lines: int = 25) -> str:
    if not path.is_file():
        return ""
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return str(pid) in (proc.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _find_artifact(job_id: str) -> Path:
    if not job_id or job_id in {".", ".."}:
        raise CliServiceError("USAGE_ERROR", "任务 id 不能为空。")
    roots = [settings.results_dir(), cli_results_dir()]
    for root in roots:
        direct = root / job_id
        if direct.is_dir():
            return direct
    # 允许只给真实任务 id 的后 8 位（app-<stamp>-<8hex> 的尾段）。
    for root in roots:
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and child.name.endswith(f"-{job_id}"):
                return child
    raise CliServiceError("JOB_NOT_FOUND", f"找不到任务 {job_id} 的证据目录。", "用 jobs list 查看可用任务。")


def _mesh_from_job(job_id: str) -> tuple[MeshRecord, Path]:
    artifact = _find_artifact(job_id)
    glb = artifact / "mesh.glb"
    if not glb.is_file():
        raise CliServiceError("NOT_READY", f"任务 {job_id} 没有可导出的网格。", "只有 DONE 任务才有 mesh.glb。")
    try:
        mesh = cast(trimesh.Trimesh, trimesh.load(str(glb), force="mesh"))
    except Exception as exc:  # noqa: BLE001 - 坏网格必须显式报错
        raise CliServiceError("EMPTY_MESH", f"无法读取网格 {glb}: {exc}") from exc
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        raise CliServiceError("EMPTY_MESH", f"任务 {job_id} 的网格为空。")
    record = MeshRecord(
        id=job_id,
        mesh=mesh,
        source="model",
        label="TripoSR",
        note="TripoSR 推理输出；由 CLI 从证据目录读回。",
    )
    return record, artifact


def _status_from_artifact(path: Path) -> dict:
    metrics = _read_json(path / "metrics.json")
    termination = _read_json(path / "termination.json")
    cancellation = _read_json(path / "cancellation.json")
    process = _read_json(path / "process.json")
    if cancellation:
        status = "CANCELLED"
    elif metrics.get("status") == "MODEL_TESTED":
        status = "DONE"
    elif termination.get("reason") == "GenerationTimeout":
        status = "TIMEOUT"
    elif termination.get("reason") == "GenerationCancelled":
        status = "CANCELLED"
    elif termination:
        status = "FAILED"
    elif process and _pid_alive(int(process.get("pid", 0) or 0)):
        status = "RUNNING"
    else:
        status = "UNKNOWN"
    return {
        "job_id": path.name,
        "status": status,
        "created_at": metrics.get("created_at") or process.get("started_at") or "",
        "wall_seconds": metrics.get("wall_seconds"),
        "error_code": metrics.get("reason") or termination.get("reason") or "",
        "has_mesh": (path / "mesh.glb").is_file(),
        "path": str(path),
    }


# ------------------------------------------------------------------ P0/P1 probe/inspect/generate
def probe_payload(probe_adapter: TripoSRAdapter | None = None) -> dict:
    probe = (probe_adapter or adapter()).probe()
    return {
        "app": APP_NAME,
        "version": VERSION,
        "state": probe.state,
        "reasons": probe.reasons,
        "warnings": probe.warnings,
        "details": probe.details,
        "model": registry.public_card(),
    }


def inspect_image(path: str, *, with_mask: bool = False) -> dict:
    source = Path(path).expanduser()
    if not source.is_file():
        raise CliServiceError("USAGE_ERROR", f"输入文件不存在：{source}")
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise CliServiceError("USAGE_ERROR", f"无法读取输入文件：{exc}") from exc
    try:
        info = validate_image_bytes(data, filename=source.name)
        hint = estimate_foreground(data)
    except ImageValidationError as exc:
        raise CliServiceError(exc.code, exc.message, details=exc.detail) from exc
    if not with_mask:
        hint.pop("mask_data_url", None)
    return {"path": str(source.resolve()), "image": info.as_dict(), "foreground": hint}


def _ready_or_raise(probe_adapter: TripoSRAdapter) -> None:
    probe = probe_adapter.probe()
    if probe.ready:
        return
    reason = probe.reasons[0] if probe.reasons else {"code": "NOT_READY", "message": "模型未就绪", "hint": ""}
    raise CliServiceError(
        str(reason.get("code", "NOT_READY")),
        str(reason.get("message", "模型未就绪，未执行推理。")),
        str(reason.get("hint", "") or registry.model_card()["download_command"]),
        {"reasons": probe.reasons},
    )


def generate_image(
    path: str,
    *,
    output_dir: str | None = None,
    mc_resolution: int = 256,
    chunk_size: int = 8192,
    remove_background: bool = True,
    foreground_ratio: float = 0.85,
    timeout_s: int | None = None,
    progress_cb: ProgressCb | None = None,
    active_adapter: TripoSRAdapter | None = None,
) -> dict:
    source = Path(path).expanduser()
    if not source.is_file():
        raise CliServiceError("USAGE_ERROR", f"输入图片不存在：{source}")
    try:
        image_bytes = source.read_bytes()
    except OSError as exc:
        raise CliServiceError("USAGE_ERROR", f"无法读取输入图片：{exc}") from exc
    try:
        info = validate_image_bytes(image_bytes, filename=source.name)
    except ImageValidationError as exc:
        raise CliServiceError(exc.code, exc.message, details=exc.detail) from exc

    active_adapter = active_adapter or adapter()
    _ready_or_raise(active_adapter)  # 0 秒拦截：不在未就绪时起子进程

    deadline = time.monotonic() + timeout_s if timeout_s else None

    def on_stage(stage: str, ratio: float) -> None:
        if progress_cb:
            progress_cb(stage, ratio)

    try:
        result: InferenceResult = active_adapter.generate(
            image_bytes,
            source.name,
            mc_resolution=mc_resolution,
            chunk_size=chunk_size,
            remove_background=remove_background,
            foreground_ratio=foreground_ratio,
            on_stage=on_stage,
            deadline=deadline,
        )
    except AdapterError as exc:
        raise CliServiceError(exc.code, exc.message, exc.hint or "", exc.detail) from exc
    except Exception as exc:  # noqa: BLE001 - 超时/取消/未知异常都要显式成码
        code = "TIMEOUT" if "超时" in str(exc) else "BUSINESS_ERROR"
        raise CliServiceError(code, f"生成失败：{type(exc).__name__}: {exc}") from exc

    artifact_dir = Path((result.timings or {}).get("artifact_dir") or "")
    job_id = artifact_dir.name if artifact_dir.name else f"job-{uuid.uuid4().hex[:12]}"
    out_dir = cli_results_dir() / job_id
    out_dir.mkdir(parents=True, exist_ok=True)

    mesh = cast(trimesh.Trimesh, result.mesh)
    record = MeshRecord(
        id=job_id,
        mesh=mesh,
        source="model",
        label="TripoSR",
        note="TripoSR 真实推理输出（CLI）。",
    )
    try:
        obj = export_obj(record)
        glb = export_glb(record)
    except Exception as exc:  # noqa: BLE001
        raise CliServiceError("EMPTY_MESH", f"网格导出失败：{exc}") from exc
    (out_dir / "mesh.obj").write_bytes(obj)
    (out_dir / "mesh.glb").write_bytes(glb)
    _write_json(
        out_dir / "summary.json",
        {
            "job_id": job_id,
            "input": str(source.resolve()),
            "image": info.as_dict(),
            "device": result.device,
            "mc_resolution": result.mc_resolution,
            "stats": mesh_stats(mesh),
            "timings": {
                k: v
                for k, v in (result.timings or {}).items()
                if k in {"device", "wall_seconds", "supervisor_wall_seconds", "supervisor_exit_code", "mesh", "status"}
            },
        },
    )
    return {
        "job_id": job_id,
        "input": str(source.resolve()),
        "output_dir": str(out_dir),
        "obj": str(out_dir / "mesh.obj"),
        "glb": str(out_dir / "mesh.glb"),
        "device": result.device,
        "stats": mesh_stats(mesh),
        "artifact_dir": str(artifact_dir) if artifact_dir.name else "",
    }


# ------------------------------------------------------------------ jobs / mesh / export
def jobs_list(limit: int = 20) -> dict:
    items: list[dict] = []
    for root in (settings.results_dir(), cli_results_dir()):
        if not root.is_dir():
            continue
        for child in root.iterdir():
            if child.is_dir() and (child.name.startswith("app-") or root.name == "cli"):
                items.append(_status_from_artifact(child))
    items.sort(key=lambda item: item["job_id"], reverse=True)
    return {"count": len(items), "jobs": items[:limit]}


def jobs_show(job_id: str) -> dict:
    artifact = _find_artifact(job_id)
    payload = _status_from_artifact(artifact)
    payload["request"] = _read_json(artifact / "request.json")
    payload["metrics"] = _read_json(artifact / "metrics.json")
    payload["summary"] = _read_json(artifact / "summary.json") or _read_json(cli_results_dir() / job_id / "summary.json")
    payload["log_tail"] = _tail(artifact / "run.log") or _tail(artifact / "supervisor.log")
    if (artifact / "mesh.glb").is_file():
        record, _ = _mesh_from_job(job_id)
        payload["mesh"] = mesh_stats(record.mesh)
    return payload


def jobs_cancel(job_id: str) -> dict:
    artifact = _find_artifact(job_id)
    status = _status_from_artifact(artifact)["status"]
    if status in {"DONE", "CANCELLED", "TIMEOUT", "FAILED"}:
        raise CliServiceError("BUSINESS_ERROR", f"任务 {job_id} 已是终态 {status}，无需取消。")
    process = _read_json(artifact / "process.json")
    pid = int(process.get("pid", 0) or 0)
    killed = False
    if _pid_alive(pid):
        if os.name == "nt":
            proc = subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
            killed = proc.returncode == 0
        else:
            try:
                os.kill(pid, 15)
                killed = True
            except OSError:
                killed = False
    _write_json(
        artifact / "cancellation.json",
        {"job_id": job_id, "pid": pid, "killed": killed, "at": time.strftime("%Y-%m-%dT%H:%M:%S")},
    )
    return {"job_id": job_id, "pid": pid, "terminated": killed, "status": "CANCELLED"}


def mesh_show(job_id: str) -> dict:
    record, artifact = _mesh_from_job(job_id)
    payload = mesh_stats(record.mesh)
    payload.update({"job_id": job_id, "source": str(artifact / "mesh.glb")})
    return payload


def mesh_export(job_id: str, fmt: str, output: str | None = None) -> dict:
    fmt = fmt.lower().strip()
    if fmt not in {"obj", "glb"}:
        raise CliServiceError("INVALID_FORMAT", f"只支持 obj / glb，收到 {fmt or '空'}。")
    record, artifact = _mesh_from_job(job_id)
    data = export_obj(record) if fmt == "obj" else export_glb(record)
    target = Path(output).expanduser() if output else (cli_results_dir() / job_id / f"mesh.{fmt}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {
        "job_id": job_id,
        "format": fmt,
        "path": str(target.resolve()),
        "bytes": len(data),
        "sha256": _sha256(data),
        "source": str(artifact / "mesh.glb"),
    }


# ------------------------------------------------------------------ P2/P3 data & ops
def models_list() -> dict:
    card = registry.public_card()
    weights_dir = settings.model_dir()
    files = []
    for entry in card["weight_files"]:
        path = weights_dir / entry["filename"]
        files.append(
            {
                "filename": entry["filename"],
                "present": path.is_file(),
                "bytes": path.stat().st_size if path.is_file() else 0,
                "expected_bytes": entry["expected_bytes"],
                "required": entry["required"],
            }
        )
    return {
        "model_id": card["id"],
        "display_name": card["display_name"],
        "verification_status": card["verification_status"],
        "weights_dir": str(weights_dir),
        "weights_bundled": card["weights_bundled"],
        "files": files,
    }


def models_show(model_id: str = "triposr") -> dict:
    card = registry.public_card()
    if model_id not in {card["id"], card["display_name"].lower()}:
        raise CliServiceError("USAGE_ERROR", f"未知模型 {model_id}。", f"可用模型：{card['id']}")
    return card


def engines_list() -> dict:
    card = registry.public_card()
    return {
        "engines": [
            {
                "id": card["id"],
                "display_name": card["display_name"],
                "role": card["role"],
                "inputs": card["inputs"],
                "outputs": card["outputs"],
                "official_vram_gb": card["official_hardware_claim"]["vram_gb"],
                "weights_bundled": False,
            }
        ]
    }


def model_status() -> dict:
    payload = probe_payload()
    return {
        "app": payload["app"],
        "version": payload["version"],
        "state": payload["state"],
        "reasons": payload["reasons"],
        "warnings": payload["warnings"],
        "weights_dir": payload["details"]["weights_dir"],
        "download_command": registry.model_card()["download_command"],
    }


def model_verify() -> dict:
    weights_dir = settings.model_dir()
    card = registry.public_card()
    checks = []
    for entry in card["weight_files"]:
        path = weights_dir / entry["filename"]
        actual = path.stat().st_size if path.is_file() else 0
        checks.append(
            {
                "filename": entry["filename"],
                "ok": actual == entry["expected_bytes"],
                "bytes": actual,
                "expected_bytes": entry["expected_bytes"],
            }
        )
    return {"weights_dir": str(weights_dir), "checks": checks, "all_ok": all(c["ok"] for c in checks)}


def model_dir() -> dict:
    return {"path": str(settings.model_dir())}


def model_download(*, execute: bool = False) -> dict:
    command = registry.model_card()["download_command"]
    if not execute:
        return {"executed": False, "command": command, "hint": "确认后加 --execute 执行；权重不入库、不入安装包。"}
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(settings.REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=1800,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliServiceError("NOT_READY", f"下载命令执行失败：{exc}", command) from exc
    if proc.returncode != 0:
        raise CliServiceError(
            "BUSINESS_ERROR",
            f"下载命令退出码 {proc.returncode}：{(proc.stderr or '').strip()[-500:]}",
            command,
        )
    return {"executed": True, "command": command, "stdout_tail": (proc.stdout or "").strip()[-1000:]}


def batch_generate(
    paths: list[str],
    *,
    output_dir: str | None = None,
    mc_resolution: int = 256,
    chunk_size: int = 8192,
    timeout_s: int | None = None,
    progress_cb: ProgressCb | None = None,
    generator: Callable[[str], dict] | None = None,
) -> dict:
    if not paths:
        raise CliServiceError("USAGE_ERROR", "batch generate 至少需要一个输入文件。")
    batch_id = f"batch-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    results: list[dict] = []
    failures: list[dict] = []
    for index, path in enumerate(paths, 1):
        if progress_cb:
            progress_cb("batch", index / len(paths))
        try:
            data = (
                generator(path)
                if generator is not None
                else generate_image(
                    path,
                    output_dir=output_dir,
                    mc_resolution=mc_resolution,
                    chunk_size=chunk_size,
                    timeout_s=timeout_s,
                    progress_cb=progress_cb,
                )
            )
            results.append({"input": path, "job_id": data["job_id"], "glb": data["glb"]})
        except CliServiceError as exc:
            failures.append({"input": path, "error_code": exc.code, "error": exc.message})
    payload = {
        "batch_id": batch_id,
        "succeeded": len(results),
        "failed": len(failures),
        "results": results,
        "failures": failures,
    }
    _write_json(cli_results_dir() / f"{batch_id}.json", payload)
    return payload


def batch_resume(batch_id: str, *, timeout_s: int | None = None) -> dict:
    path = cli_results_dir() / f"{batch_id}.json"
    data = _read_json(path)
    if not data:
        raise CliServiceError("JOB_NOT_FOUND", f"找不到批次 {batch_id}。")
    pending = [item["input"] for item in data.get("failures", []) if item.get("input")]
    if not pending:
        return {"batch_id": batch_id, "resumed": 0, "message": "没有需要重试的失败项。"}
    resumed = batch_generate(pending, timeout_s=timeout_s)
    resumed["resumed"] = len(pending)
    resumed["resumed_from"] = batch_id
    return resumed


# ------------------------------------------------------------------ P4 system / serve
_REDACT_KEYS = ("token", "secret", "password", "credential", "authorization", "apikey", "api_key")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("***REDACTED***" if any(word in str(key).lower() for word in _REDACT_KEYS) else _redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def system_health() -> dict:
    payload = probe_payload()
    return {
        "app": APP_NAME,
        "version": VERSION,
        "state": payload["state"],
        "reasons": payload["reasons"],
        "results_dir": str(settings.results_dir()),
        "cli_results_dir": str(cli_results_dir()),
    }


def system_gpu() -> dict:
    payload = probe_payload()["details"].get("runtime", {}).get("probe", {})
    info: dict[str, Any] = {
        "runtime_python": str(settings.runtime_python()),
        "cuda_available": bool(payload.get("cuda_available")),
        "device_name": payload.get("device_name"),
        "torch": payload.get("torch"),
        "nvidia_smi": None,
    }
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,memory.used,utilization.gpu",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=6,
            check=False,
        )
        if proc.returncode == 0:
            info["nvidia_smi"] = (proc.stdout or "").strip().splitlines()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return info


def system_config() -> dict:
    return _redact(
        {
            "app": APP_NAME,
            "version": VERSION,
            "model_dir": str(settings.model_dir()),
            "results_dir": str(settings.results_dir()),
            "upstream_dir": str(settings.upstream_dir()),
            "runtime_python": str(settings.runtime_python()),
            "supervisor": str(settings.supervisor_script()),
            "min_free_mib": settings.min_free_mib(),
            "job_timeout_s": settings.job_timeout_s(),
            "max_image_bytes": settings.max_image_bytes(),
            "probe_timeout_s": settings.runtime_probe_timeout_s(),
            "cli_results_dir": str(cli_results_dir()),
        }
    )


def serve(*, host: str, port: int, timeout_s: int, log_level: str = "info") -> dict:
    if host not in LOOPBACK_HOSTS:
        raise CliServiceError(
            "USAGE_ERROR",
            f"拒绝绑定非回环地址 {host}；只允许 127.0.0.1 / localhost / ::1。",
        )
    try:
        import uvicorn

        from triposr_lite.app import create_app
    except ImportError as exc:
        raise CliServiceError("NOT_READY", f"启动服务依赖缺失：{exc}") from exc
    app = create_app(timeout_s=timeout_s)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
    return {"host": host, "port": port, "timeout_s": timeout_s}


__all__ = [
    "CliServiceError",
    "adapter",
    "batch_generate",
    "batch_resume",
    "cli_results_dir",
    "engines_list",
    "generate_image",
    "inspect_image",
    "jobs_cancel",
    "jobs_list",
    "jobs_show",
    "mesh_export",
    "mesh_show",
    "model_dir",
    "model_download",
    "model_status",
    "model_verify",
    "models_list",
    "models_show",
    "probe_payload",
    "serve",
    "system_config",
    "system_gpu",
    "system_health",
]
