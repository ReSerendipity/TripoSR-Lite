"""TripoSR adapter: readiness probe and the real inference call path.

Nothing here pretends to have run a model. If weights or upstream code are
missing, the adapter reports NOT_READY with machine-readable reasons. The
inference path below is the upstream call sequence (TSR.from_pretrained →
forward → extract_mesh); it is only executed when the environment is actually
ready.
"""

from __future__ import annotations

import importlib.util
import io
import platform
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

from . import registry, settings

STAGES = ("load_model", "preprocess", "forward", "extract")

_runtime_cache: dict = {"at": 0.0, "data": None, "ttl": 15.0}


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


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, AttributeError, ValueError, ModuleNotFoundError):
        return False


def _torch_details() -> dict:
    now = time.monotonic()
    if _runtime_cache["data"] is not None and now - _runtime_cache["at"] < _runtime_cache["ttl"]:
        return dict(_runtime_cache["data"])

    if not _module_available("torch"):
        data = {"available": False, "version": None, "cuda_available": False, "device_name": None}
    else:
        try:
            import torch

            cuda = bool(torch.cuda.is_available())
            data = {
                "available": True,
                "version": str(torch.__version__),
                "cuda_available": cuda,
                "cuda_version": str(torch.version.cuda) if torch.version.cuda else None,
                "device_name": torch.cuda.get_device_name(0) if cuda else None,
            }
        except Exception as exc:  # noqa: BLE001 - a broken torch install is a valid probe result
            data = {"available": False, "version": None, "cuda_available": False, "device_name": None, "error": str(exc)}

    _runtime_cache["data"] = data
    _runtime_cache["at"] = now
    return dict(data)


def _reset_runtime_cache() -> None:
    """Test hook: forget cached torch probe results."""
    _runtime_cache["data"] = None
    _runtime_cache["at"] = 0.0


class TripoSRAdapter:
    def __init__(self, weights_dir: Path | str | None = None, min_ckpt_bytes: int | None = None):
        self._weights_dir = Path(weights_dir) if weights_dir else None
        self._min_ckpt_bytes = min_ckpt_bytes

    def weights_dir(self) -> Path:
        return self._weights_dir or settings.model_dir()

    def min_ckpt_bytes(self) -> int:
        return self._min_ckpt_bytes if self._min_ckpt_bytes is not None else settings.min_model_ckpt_bytes()

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

        torch_info = _torch_details()
        if not torch_info["available"]:
            reasons.append(
                {
                    "code": "MISSING_TORCH",
                    "message": "当前 Python 环境没有可用的 torch",
                    "hint": "建议 Python 3.10/3.11 + 与本机 CUDA 大版本一致的 PyTorch。",
                }
            )
        elif not torch_info["cuda_available"]:
            warnings.append(
                {
                    "code": "NO_CUDA",
                    "message": "torch 未检测到 CUDA，只能用 CPU 推理（会非常慢）。",
                }
            )

        upstream_available = _module_available("tsr")
        if not upstream_available:
            reasons.append(
                {
                    "code": "MISSING_UPSTREAM_CODE",
                    "message": "未安装上游 tsr 包（TripoSR 官方推理代码）",
                    "hint": registry.model_card()["upstream_install_command"],
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
            "upstream": {"available": upstream_available, "module": "tsr",
                         "install_command": card["upstream_install_command"]},
            "torch": torch_info,
            "python": sys.version.split()[0],
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
    def generate(
        self,
        image_bytes: bytes,
        filename: str = "upload.png",
        *,
        mc_resolution: int = 256,
        chunk_size: int = 8192,
        remove_background: bool = True,
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

        timings: dict = {}
        try:
            checkpoint("load_model", 0.05)
            started = time.perf_counter()
            import torch  # noqa: PLC0415 - lazy by design
            from tsr.system import TSR  # noqa: PLC0415

            device = "cuda" if torch.cuda.is_available() else "cpu"
            model = TSR.from_pretrained(
                str(self.weights_dir()), config_name="config.yaml", weight_name="model.ckpt"
            )
            model.renderer.set_chunk_size(chunk_size)
            model.to(device)
            model.eval()
            timings["load_model"] = round(time.perf_counter() - started, 3)
            checkpoint("preprocess", 0.30)

            started = time.perf_counter()
            image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
            if remove_background:
                import rembg  # noqa: PLC0415
                from tsr.utils import remove_background as tsr_remove_background
                from tsr.utils import resize_foreground

                session = rembg.new_session()
                cutout = tsr_remove_background(image, session)
                cutout = resize_foreground(cutout, 0.85)
                array = np.asarray(cutout).astype(np.float32) / 255.0
                array = array[:, :, :3] * array[:, :, 3:4] + (1.0 - array[:, :, 3:4]) * 0.5
                image = Image.fromarray((array * 255.0).astype(np.uint8))
            timings["preprocess"] = round(time.perf_counter() - started, 3)
            checkpoint("forward", 0.40)

            started = time.perf_counter()
            with torch.no_grad():
                scene_codes = model([image], device=device)
            timings["forward"] = round(time.perf_counter() - started, 3)
            checkpoint("extract", 0.70)

            started = time.perf_counter()
            meshes = model.extract_mesh(scene_codes, True, resolution=mc_resolution)
            timings["extract"] = round(time.perf_counter() - started, 3)
            if not meshes:
                raise AdapterError("EMPTY_MESH", "上游推理没有返回网格。")
            mesh = meshes[0]
            timings["device"] = device
            checkpoint("done", 1.0)
            return InferenceResult(mesh=mesh, device=device, timings=timings, mc_resolution=mc_resolution)
        except (GenerationCancelled, GenerationTimeout, AdapterError):
            raise
        except Exception as exc:  # noqa: BLE001 - keep the honest failure reason
            raise AdapterError(
                "INFERENCE_FAILED",
                f"{type(exc).__name__}: {exc}",
                {"traceback": traceback.format_exc(limit=6), "filename": filename},
                hint="常见原因：torchmcubes 未编译 CUDA 扩展、CUDA 版本与 torch 不匹配、显存不足。",
            ) from exc
