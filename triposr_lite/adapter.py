"""TripoSR adapter: readiness probe and the out-of-process inference call path.

The application process never imports torch. Readiness is checked with a short
probe inside the external runtime interpreter, and generation executes the
existing supervisor script (scripts/run_triposr_real.py) as a child process,
then reads the mesh it produced. Failures keep the per-job artifact directory.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import trimesh

from . import registry, settings

STAGES = ("load_model", "preprocess", "forward", "extract")

STAGE_SEQUENCE = STAGES
STAGE_MARKERS = {
    "load_model": "Initializing model ...",
    "preprocess": "Processing images ...",
    "forward": "Running model ...",
    "extract": "Extracting mesh ...",
}
STAGE_PROGRESS = {"load_model": 0.05, "preprocess": 0.30, "forward": 0.40, "extract": 0.70}

PROBE_JSON_PREFIX = "PROBE_JSON "

# Runs inside the external runtime; prints one machine-readable line on success.
PROBE_SCRIPT = """
import json
import sys
import traceback

payload = {"python": sys.version.split()[0]}

try:
    import torch
except Exception:
    traceback.print_exc()
    print("PROBE_FAILED: MISSING_TORCH")
    sys.exit(3)

payload["torch"] = torch.__version__
payload["cuda_available"] = bool(torch.cuda.is_available())
payload["device_name"] = torch.cuda.get_device_name(0) if payload["cuda_available"] else None

try:
    import tsr.system  # noqa: F401
except Exception:
    traceback.print_exc()
    print("PROBE_FAILED: MISSING_UPSTREAM_CODE")
    sys.exit(4)

print("PROBE_JSON " + json.dumps(payload))
"""

_runtime_cache: dict = {"at": 0.0, "key": None, "data": None}


class AdapterError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
        self.hint = hint

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail, "hint": self.hint}


class GenerationCancelled(Exception):
    pass


class GenerationTimeout(Exception):
    pass


@dataclass
class ProbeResult:
    state: str
    reasons: list[dict] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    details: dict = field(default_factory=dict)

    @property
    def ready(self) -> bool:
        return self.state == "READY"

    def as_dict(self) -> dict:
        return {
            "state": self.state,
            "ready": self.ready,
            "reasons": self.reasons,
            "warnings": self.warnings,
            "details": self.details,
        }


@dataclass
class InferenceResult:
    mesh: object
    device: str
    timings: dict
    mc_resolution: int


def _tail(text: str, lines: int = 20) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])


def classify_probe_failure(text: str) -> str:
    """Map a failed runtime probe to the stable reason codes."""
    lowered = (text or "").lower()
    if "probe_failed: missing_torch" in lowered or "no module named 'torch'" in lowered:
        return "MISSING_TORCH"
    if (
        "probe_failed: missing_upstream_code" in lowered
        or "no module named 'tsr'" in lowered
        or "no module named 'torchmcubes'" in lowered
    ):
        return "MISSING_UPSTREAM_CODE"
    return "RUNTIME_PROBE_FAILED"


def _probe_runtime_process(python: Path, upstream_dir: Path, timeout: float) -> dict:
    """Run the import probe in the external interpreter; never imports torch here."""
    base = {"ok": False, "python": str(python), "upstream_dir": str(upstream_dir)}
    if not python.is_file():
        return {**base, "code": "MISSING_TORCH", "error": f"外部运行时 Python 不存在：{python}"}

    env = os.environ.copy()
    env["PYTHONPATH"] = str(settings.COMPAT_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [str(python), "-c", PROBE_SCRIPT],
            cwd=str(upstream_dir),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {**base, "code": "RUNTIME_PROBE_FAILED", "error": f"外部运行时探测超时（{timeout:.0f}s）"}
    except OSError as exc:
        return {**base, "code": "RUNTIME_PROBE_FAILED", "error": f"{type(exc).__name__}: {exc}"}

    output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
    if proc.returncode != 0:
        return {
            **base,
            "code": classify_probe_failure(output),
            "error": _tail(output),
            "exit_code": proc.returncode,
        }

    payload = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith(PROBE_JSON_PREFIX):
            try:
                payload = json.loads(line[len(PROBE_JSON_PREFIX):])
            except json.JSONDecodeError:
                payload = None
    if payload is None:
        return {**base, "code": "RUNTIME_PROBE_FAILED", "error": "探测输出缺少 PROBE_JSON", "output": _tail(output)}

    return {
        **base,
        "ok": True,
        "code": "OK",
        "torch": payload.get("torch"),
        "cuda_available": bool(payload.get("cuda_available")),
        "device_name": payload.get("device_name"),
        "runtime_python_version": payload.get("python"),
    }


# Overridable seam used by probe(); tests replace it to avoid spawning the runtime.
_external_runtime_probe = _probe_runtime_process


def _reset_runtime_cache() -> None:
    """Test hook: forget the cached runtime probe result."""
    _runtime_cache["data"] = None
    _runtime_cache["key"] = None
    _runtime_cache["at"] = 0.0


def stage_updates(log_text: str, reached: int) -> tuple[int, list[tuple[str, float]]]:
    """Map supervisor stage markers to adapter stages, in order, never backwards."""
    updates: list[tuple[str, float]] = []
    for index, stage in enumerate(STAGE_SEQUENCE):
        if index < reached:
            continue
        if STAGE_MARKERS[stage] in (log_text or ""):
            for missing in range(reached, index + 1):
                name = STAGE_SEQUENCE[missing]
                updates.append((name, STAGE_PROGRESS[name]))
            reached = index + 1
    return reached, updates


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _sanitize_suffix(filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    return suffix if suffix in {".png", ".jpg", ".jpeg"} else ".png"


class TripoSRAdapter:
    def __init__(
        self,
        weights_dir: Path | str | None = None,
        min_ckpt_bytes: int | None = None,
        *,
        runtime_python: Path | str | None = None,
        supervisor: Path | str | None = None,
        upstream_dir: Path | str | None = None,
        results_dir: Path | str | None = None,
        min_free_mib: int | None = None,
    ):
        self._weights_dir = Path(weights_dir) if weights_dir else None
        self._min_ckpt_bytes = min_ckpt_bytes
        self._runtime_python = Path(runtime_python) if runtime_python else None
        self._supervisor = Path(supervisor) if supervisor else None
        self._upstream_dir = Path(upstream_dir) if upstream_dir else None
        self._results_dir = Path(results_dir) if results_dir else None
        self._min_free_mib = min_free_mib

    def weights_dir(self) -> Path:
        return self._weights_dir or settings.model_dir()

    def min_ckpt_bytes(self) -> int:
        return self._min_ckpt_bytes if self._min_ckpt_bytes is not None else settings.min_model_ckpt_bytes()

    def runtime_python(self) -> Path:
        return self._runtime_python or settings.runtime_python()

    def supervisor_path(self) -> Path:
        return self._supervisor or settings.supervisor_script()

    def upstream_path(self) -> Path:
        return self._upstream_dir or settings.upstream_dir()

    def results_path(self) -> Path:
        return self._results_dir or settings.results_dir()

    def min_free_mib_value(self) -> int:
        return self._min_free_mib if self._min_free_mib is not None else settings.min_free_mib()

    # ------------------------------------------------------------------ probe
    def _runtime_probe(self, python: Path, upstream: Path) -> dict:
        key = (str(python), str(upstream), str(settings.COMPAT_DIR))
        now = time.monotonic()
        ttl = settings.runtime_probe_ttl_s()
        if (
            _runtime_cache["data"] is not None
            and _runtime_cache["key"] == key
            and now - _runtime_cache["at"] < ttl
        ):
            return dict(_runtime_cache["data"])
        data = _external_runtime_probe(python, upstream, settings.runtime_probe_timeout_s())
        _runtime_cache.update({"at": now, "key": key, "data": data})
        return dict(data)

    def probe(self) -> ProbeResult:
        weights = self.weights_dir()
        config_path = weights / "config.yaml"
        weight_path = weights / "model.ckpt"

        reasons: list[dict] = []
        warnings: list[dict] = []

        config_present = config_path.is_file()
        weight_present = weight_path.is_file()
        weight_bytes = weight_path.stat().st_size if weight_present else 0

        if not config_present:
            reasons.append(
                {
                    "code": "MISSING_CONFIG",
                    "message": f"缺少 {config_path}",
                    "hint": "config.yaml 与 model.ckpt 需放在同一模型目录。",
                }
            )
        if not weight_present:
            reasons.append(
                {
                    "code": "MISSING_WEIGHTS",
                    "message": f"缺少 {weight_path}（官方 1.68 GB）",
                    "hint": registry.model_card()["download_command"],
                }
            )
        elif weight_bytes < self.min_ckpt_bytes():
            reasons.append(
                {
                    "code": "WEIGHT_TOO_SMALL",
                    "message": f"model.ckpt 只有 {weight_bytes / 1048576:.1f} MB，疑似下载不完整",
                    "hint": f"官方文件约 {registry.model_card()['weight_files'][0]['expected_label']}，请重新下载并核对大小。",
                }
            )

        upstream = self.upstream_path()
        run_py = upstream / "run.py"
        supervisor = self.supervisor_path()
        python = self.runtime_python()

        if not run_py.is_file():
            reasons.append(
                {
                    "code": "MISSING_UPSTREAM_CODE",
                    "message": f"缺少上游推理入口 {run_py}",
                    "hint": registry.model_card()["upstream_install_command"],
                }
            )
        if not supervisor.is_file():
            reasons.append(
                {
                    "code": "MISSING_UPSTREAM_CODE",
                    "message": f"缺少监督脚本 {supervisor}",
                    "hint": "scripts/run_triposr_real.py 属于本仓库代码，可用 TRIPOSR_SUPERVISOR 指向别处。",
                }
            )

        runtime = self._runtime_probe(python, upstream)
        if runtime.get("ok"):
            if not runtime.get("cuda_available"):
                warnings.append(
                    {
                        "code": "NO_CUDA",
                        "message": "外部运行时 torch 未检测到 CUDA，只能用 CPU 推理（会非常慢）。",
                    }
                )
        else:
            reasons.append(
                {
                    "code": runtime.get("code") or "RUNTIME_PROBE_FAILED",
                    "message": runtime.get("error") or f"外部运行时 {python} 探测失败",
                    "hint": "在该运行时里安装 torch 与上游 tsr（pip install -e external/TripoSR），再用 TRIPOSR_RUNTIME_PYTHON 指定解释器。",
                }
            )

        card = registry.model_card()
        details = {
            "weights_dir": str(weights),
            "config": {"path": str(config_path), "present": config_present,
                       "bytes": config_path.stat().st_size if config_present else 0},
            "weight": {
                "path": str(weight_path),
                "present": weight_present,
                "bytes": weight_bytes,
                "expected_bytes": card["weight_files"][0]["expected_bytes"],
                "min_bytes": self.min_ckpt_bytes(),
            },
            "upstream": {"dir": str(upstream), "run_py": str(run_py), "present": run_py.is_file()},
            "supervisor": {"path": str(supervisor), "present": supervisor.is_file()},
            "runtime": {
                "python": str(python),
                "present": python.is_file(),
                "probe": runtime,
                "compat_dir": str(settings.COMPAT_DIR),
            },
            "app_python": sys.version.split()[0],
            "app_process_torch_imported": False,
            "min_free_mib": self.min_free_mib_value(),
            "platform": platform.platform(),
            "official_vram_gb": card["official_hardware_claim"]["vram_gb"],
            "download_command": card["download_command"],
            "weights_url": card["upstream_weights_url"],
            "code_url": card["upstream_code_url"],
            "verification_status": card["verification_status"],
        }

        return ProbeResult(
            state="READY" if not reasons else "NOT_READY",
            reasons=reasons,
            warnings=warnings,
            details=details,
        )

    # ------------------------------------------------------------------ inference
    def _new_artifact_dir(self) -> Path:
        root = self.results_path()
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = root / f"app-{stamp}-{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=False)
        return path

    def _command(self, image_path: Path, out_dir: Path, mc_resolution: int, chunk_size: int,
                 remove_background: bool, foreground_ratio: float, device: str) -> list[str]:
        cmd = [
            str(self.runtime_python()),
            str(self.supervisor_path()),
            "--image", str(image_path),
            "--output-dir", str(out_dir),
            "--mc-resolution", str(int(mc_resolution)),
            "--chunk-size", str(int(chunk_size)),
            "--min-free-mib", str(int(self.min_free_mib_value())),
            "--model-dir", str(self.weights_dir()),
            "--device", device,
        ]
        if remove_background:
            cmd += ["--foreground-ratio", f"{float(foreground_ratio):.3f}"]
        else:
            cmd += ["--no-remove-bg"]
        return cmd

    def _terminate_tree(self, proc: subprocess.Popen, grace_s: float = 5.0) -> dict:
        if proc.poll() is not None:
            return {"already_exited": True}
        if os.name == "nt":
            # taskkill /T covers the supervisor's own run.py child.
            try:
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, text=True, timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                proc.terminate()
        try:
            proc.wait(timeout=grace_s)
            escalated = False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
            escalated = True
        return {"already_exited": False, "escalated_to_kill": escalated}

    def _failure_from_metrics(self, out_dir: Path, metrics: dict, exit_code: int) -> AdapterError:
        status = str(metrics.get("status") or "")
        reason = str(metrics.get("reason") or "")
        detail = {
            "artifact_dir": str(out_dir),
            "exit_code": exit_code,
            "status": status,
            "reason": reason,
            "metrics": {k: metrics.get(k) for k in ("gpu_before", "log_tail", "process")},
        }
        lowered = reason.lower()
        if status == "BLOCKED" and "gpu busy" in lowered:
            return AdapterError("GPU_BUSY", f"GPU 被其他计算进程占用：{reason}", detail,
                                hint="等待占用显卡的进程结束后重试；监督脚本不会抢占或降级。")
        if status == "BLOCKED" and ("insufficient vram" in lowered or "vram" in lowered):
            return AdapterError("INSUFFICIENT_VRAM", f"空闲显存不足：{reason}", detail,
                                hint="检查 nvidia-smi 空闲显存是否达到 min_free_mib。")
        return AdapterError(
            "SUPERVISOR_FAILED",
            f"监督脚本失败（退出码 {exit_code}，status={status or 'missing'}）：{reason or '无原因字段'}",
            detail,
            hint=f"证据保留在 {out_dir}（metrics.json / run.log / supervisor.log）。",
        )

    def generate(
        self,
        image_bytes: bytes,
        filename: str = "upload.png",
        *,
        mc_resolution: int = 256,
        chunk_size: int = 8192,
        remove_background: bool = True,
        foreground_ratio: float = 0.85,
        on_stage: Callable[[str, float], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> InferenceResult:
        probe = self.probe()
        if not probe.ready:
            raise AdapterError(
                "NOT_READY",
                "模型未就绪，未执行推理。",
                {"reasons": probe.reasons, "weights_dir": probe.details["weights_dir"]},
                hint=registry.model_card()["download_command"],
            )

        def checkpoint(stage: str, progress: float) -> None:
            if should_cancel is not None and should_cancel():
                raise GenerationCancelled(f"任务在阶段 {stage} 被取消")
            if deadline is not None and time.monotonic() > deadline:
                raise GenerationTimeout(f"任务在阶段 {stage} 超时")
            if on_stage is not None:
                on_stage(stage, progress)

        out_dir = self._new_artifact_dir()
        image_path = out_dir / f"input{_sanitize_suffix(filename)}"
        image_path.write_bytes(image_bytes)

        requested_device = os.environ.get("TRIPOSR_DEVICE") or (
            "cuda:0" if probe.details.get("runtime", {}).get("probe", {}).get("cuda_available") else "cpu"
        )
        cmd = self._command(
            image_path, out_dir, mc_resolution, chunk_size, remove_background, foreground_ratio, requested_device
        )
        _write_json(
            out_dir / "request.json",
            {
                "filename": filename,
                "image": str(image_path),
                "image_bytes": len(image_bytes),
                "mc_resolution": int(mc_resolution),
                "chunk_size": int(chunk_size),
                "remove_background": bool(remove_background),
                "foreground_ratio": float(foreground_ratio),
                "min_free_mib": int(self.min_free_mib_value()),
                "argv": cmd,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        popen_kw: dict = {}
        if os.name == "nt":
            popen_kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kw["start_new_session"] = True

        try:
            checkpoint("load_model", STAGE_PROGRESS["load_model"])
        except (GenerationCancelled, GenerationTimeout) as exc:
            _write_json(
                out_dir / "termination.json",
                {
                    "reason": type(exc).__name__,
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "supervisor_started": False,
                },
            )
            raise

        log_path = out_dir / "supervisor.log"
        log_fh = log_path.open("w", encoding="utf-8")
        proc: subprocess.Popen | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(settings.REPO_ROOT),
                env=env,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                **popen_kw,
            )
            run_log = out_dir / "run.log"
            reached = 1  # load_model was already reported before the launch
            while True:
                if should_cancel is not None and should_cancel():
                    raise GenerationCancelled("任务被取消，监督脚本子进程已终止")
                if deadline is not None and time.monotonic() > deadline:
                    raise GenerationTimeout("任务超时，监督脚本子进程已终止")
                if proc.poll() is not None:
                    break
                reached, updates = stage_updates(_read_text(run_log), reached)
                for stage, progress in updates:
                    checkpoint(stage, progress)
                time.sleep(0.2)
            reached, updates = stage_updates(_read_text(run_log), reached)
            for stage, progress in updates:
                checkpoint(stage, progress)
            exit_code = proc.returncode
        except (GenerationCancelled, GenerationTimeout) as exc:
            termination = {
                "reason": type(exc).__name__,
                "at": datetime.now().isoformat(timespec="seconds"),
                "supervisor_started": proc is not None,
            }
            if proc is not None:
                termination["pid"] = proc.pid
                termination.update(self._terminate_tree(proc))
            _write_json(out_dir / "termination.json", termination)
            raise
        finally:
            if proc is not None and proc.poll() is None:
                self._terminate_tree(proc)
            log_fh.close()

        metrics_path = out_dir / "metrics.json"
        metrics = {}
        if metrics_path.is_file():
            try:
                metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                metrics = {}

        if not metrics:
            raise AdapterError(
                "SUPERVISOR_FAILED",
                f"监督脚本未产出可解析的 metrics.json（退出码 {exit_code}）",
                {"artifact_dir": str(out_dir), "exit_code": exit_code, "log_tail": _tail(_read_text(log_path))},
                hint=f"证据保留在 {out_dir}。",
            )

        if exit_code != 0 or str(metrics.get("status")) != "MODEL_TESTED":
            raise self._failure_from_metrics(out_dir, metrics, exit_code)

        glb = out_dir / "mesh.glb"
        if not glb.is_file():
            raise AdapterError(
                "EMPTY_MESH",
                f"监督脚本报告成功但没有 {glb}",
                {"artifact_dir": str(out_dir), "exit_code": exit_code},
            )
        try:
            mesh = trimesh.load(str(glb), force="mesh")
        except Exception as exc:  # noqa: BLE001 - unreadable mesh is a real failure
            raise AdapterError(
                "EMPTY_MESH",
                f"无法读取 {glb}：{type(exc).__name__}: {exc}",
                {"artifact_dir": str(out_dir), "traceback": traceback.format_exc(limit=4)},
            ) from exc
        if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
            raise AdapterError(
                "EMPTY_MESH",
                "监督脚本产出的网格顶点或面为空。",
                {"artifact_dir": str(out_dir), "vertices": len(mesh.vertices), "faces": len(mesh.faces)},
            )

        checkpoint("done", 1.0)

        params = metrics.get("params") or {}
        timings: dict = dict(metrics.get("timings") or {})
        timings.update(
            {
                "device": params.get("device_effective") or "unknown",
                "device_requested": params.get("device"),
                "artifact_dir": str(out_dir),
                "wall_seconds": metrics.get("wall_seconds"),
                "supervisor_wall_seconds": metrics.get("upstream_wall_seconds"),
                "supervisor_exit_code": exit_code,
                "gpu": metrics.get("gpu") or {},
                "mesh": {"vertices": int(len(mesh.vertices)), "faces": int(len(mesh.faces))},
                "status": metrics.get("status"),
                "supervisor_log": str(log_path),
                "run_log": str(out_dir / "run.log"),
            }
        )
        return InferenceResult(
            mesh=mesh,
            device=timings["device"],
            timings=timings,
            mc_resolution=mc_resolution,
        )
