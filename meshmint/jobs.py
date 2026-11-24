"""Job lifecycle: QUEUED → RUNNING → DONE / FAILED / TIMEOUT / CANCELLED.

The NOT_READY branch is a first-class terminal state: it never fabricates a
successful inference, it only exposes the demo mesh with an explicit label.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from . import settings
from .adapter import (
    AdapterError,
    GenerationCancelled,
    GenerationTimeout,
    InferenceResult,
    TripoSRAdapter,
)
from .imaging import ImageInfo
from .meshes import DEMO_LABEL, DEMO_NOTE, MeshRecord, utc_now
from .store import MeshStore

STAGE_LABELS: dict[str, tuple[str, float]] = {
    "queued": ("排队", 0.0),
    "load_model": ("加载 TripoSR", 0.25),
    "preprocess": ("抠图与缩放", 0.40),
    "forward": ("前向重建", 0.70),
    "extract": ("提取网格", 0.92),
    "done": ("完成", 1.0),
    "not_ready": ("模型未就绪", 0.0),
}


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    NOT_READY = "NOT_READY"
    DONE = "DONE"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"

    @property
    def terminal(self) -> bool:
        return self in {
            JobStatus.NOT_READY,
            JobStatus.DONE,
            JobStatus.FAILED,
            JobStatus.TIMEOUT,
            JobStatus.CANCELLED,
        }


@dataclass
class Job:
    id: str
    status: JobStatus
    image: dict
    created_at: str
    updated_at: str
    message: str = ""
    stage: str = "queued"
    stage_label: str = "排队"
    progress: float = 0.0
    error_code: str | None = None
    error_detail: dict | None = None
    mesh_id: str | None = None
    demo: bool = False
    duration_s: float | None = None
    timeout_s: int = settings.DEFAULT_JOB_TIMEOUT_S
    cancel_requested: bool = False
    timings: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        payload = {
            "id": self.id,
            "status": self.status.value,
            "terminal": self.status.terminal,
            "message": self.message,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "progress": round(self.progress, 4),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
            "mesh_id": self.mesh_id,
            "demo": self.demo,
            "duration_s": self.duration_s,
            "timeout_s": self.timeout_s,
            "cancel_requested": self.cancel_requested,
            "timings": self.timings,
            "image": self.image,
        }
        if self.mesh_id:
            payload["mesh_url"] = f"/api/jobs/{self.id}/mesh"
            payload["export"] = {
                "obj": f"/api/jobs/{self.id}/export?format=obj",
                "glb": f"/api/jobs/{self.id}/export?format=glb",
            }
        return payload


Generator = Callable[..., InferenceResult]


class JobManager:
    def __init__(
        self,
        adapter: TripoSRAdapter,
        store: MeshStore,
        demo_record: MeshRecord,
        timeout_s: int | None = None,
        generator: Generator | None = None,
    ) -> None:
        self.adapter = adapter
        self.store = store
        self.demo_record = demo_record
        self.timeout_s = timeout_s if timeout_s is not None else settings.job_timeout_s()
        self._generator = generator
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}

    # ------------------------------------------------------------------ creation
    def _new_job(self, image: ImageInfo) -> Job:
        now = utc_now()
        return Job(
            id=uuid.uuid4().hex[:12],
            status=JobStatus.QUEUED,
            image=image.as_dict(),
            created_at=now,
            updated_at=now,
            timeout_s=self.timeout_s,
        )

    def create_not_ready(self, image: ImageInfo, probe_reasons: list[dict]) -> Job:
        job = self._new_job(image)
        job.status = JobStatus.NOT_READY
        job.stage = "not_ready"
        job.stage_label = STAGE_LABELS["not_ready"][0]
        job.progress = 0.0
        job.demo = True
        job.mesh_id = self.demo_record.id
        codes = ", ".join(reason["code"] for reason in probe_reasons) or "NOT_READY"
        job.message = (
            f"模型未就绪（{codes}）：未执行 TripoSR 推理。"
            "预览区展示的是标注为 DEMO 的演示网格。"
        )
        job.error_code = "NOT_READY"
        job.error_detail = {"reasons": probe_reasons}
        with self._lock:
            self._jobs[job.id] = job
        return job

    def submit(self, image: ImageInfo, image_bytes: bytes) -> Job:
        probe = self.adapter.probe()
        if not probe.ready:
            return self.create_not_ready(image, probe.reasons)

        job = self._new_job(image)
        with self._lock:
            self._jobs[job.id] = job
        thread = threading.Thread(
            target=self._run,
            args=(job, image_bytes),
            name=f"meshmint-job-{job.id}",
            daemon=True,
        )
        thread.start()
        return job

    # ------------------------------------------------------------------ worker
    def _set(self, job: Job, **changes) -> None:
        with self._lock:
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = utc_now()

    def _run(self, job: Job, image_bytes: bytes) -> None:
        started = time.monotonic()
        deadline = started + self.timeout_s

        def should_cancel() -> bool:
            with self._lock:
                return job.cancel_requested

        def on_stage(stage: str, progress: float) -> None:
            label, floor = STAGE_LABELS.get(stage, (stage, float(progress)))
            self._set(
                job,
                stage=stage,
                stage_label=label,
                progress=max(float(progress), float(floor)),
            )

        if should_cancel():
            self._set(job, status=JobStatus.CANCELLED, message="任务在开始前被取消。", progress=0.0)
            return

        self._set(job, status=JobStatus.RUNNING, stage="load_model", stage_label=STAGE_LABELS["load_model"][0])

        generator = self._generator or self._default_generate

        try:
            result: InferenceResult = generator(
                image_bytes=image_bytes,
                filename=job.image.get("filename", "upload"),
                on_stage=on_stage,
                should_cancel=should_cancel,
                deadline=deadline,
                timeout_s=self.timeout_s,
            )
            if time.monotonic() > deadline:
                # The stage-boundary checks live in the adapter; this guard also
                # covers a generator that finished late without checking its budget.
                raise GenerationTimeout("任务超出时限，结果已丢弃（不写入网格）")
            record = MeshRecord(
                id=f"job-{job.id}",
                mesh=result.mesh,
                source="model",
                label="TripoSR",
                note="上游 TripoSR 真实推理输出（本机执行）。",
                job_id=job.id,
            )
            self.store.put(record)
            duration = round(time.monotonic() - started, 2)
            self._set(
                job,
                status=JobStatus.DONE,
                stage="done",
                stage_label=STAGE_LABELS["done"][0],
                progress=1.0,
                mesh_id=record.id,
                demo=False,
                duration_s=duration,
                message=f"完成：{len(result.mesh.vertices)} 顶点 / {len(result.mesh.faces)} 面，{result.device} 推理。",
                timings=getattr(result, "timings", {}),
            )
        except GenerationCancelled as exc:
            self._set(
                job,
                status=JobStatus.CANCELLED,
                message=f"已取消：{exc}。GPU 单次前向不可抢占，取消只在阶段边界生效。",
                cancel_requested=True,
                duration_s=round(time.monotonic() - started, 2),
            )
        except GenerationTimeout as exc:
            self._set(
                job,
                status=JobStatus.TIMEOUT,
                error_code="TIMEOUT",
                message=f"超时：{exc}。超时同样只在阶段边界生效。",
                duration_s=round(time.monotonic() - started, 2),
            )
        except AdapterError as exc:
            self._set(
                job,
                status=JobStatus.FAILED,
                error_code=exc.code,
                error_detail=exc.as_dict(),
                message=f"失败：{exc.message}",
                duration_s=round(time.monotonic() - started, 2),
            )
        except Exception as exc:  # noqa: BLE001 - never report a false success
            self._set(
                job,
                status=JobStatus.FAILED,
                error_code="UNEXPECTED_ERROR",
                error_detail={"type": type(exc).__name__, "message": str(exc)},
                message=f"失败：{type(exc).__name__}: {exc}",
                duration_s=round(time.monotonic() - started, 2),
            )

    def _default_generate(self, image_bytes, filename, on_stage, should_cancel, deadline, timeout_s):
        return self.adapter.generate(
            image_bytes,
            filename,
            on_stage=on_stage,
            should_cancel=should_cancel,
            deadline=deadline,
        )

    # ------------------------------------------------------------------ queries
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.get(job_id)
        if job is None:
            return None
        with self._lock:
            if job.status.terminal:
                return job
            job.cancel_requested = True
            if job.status == JobStatus.QUEUED:
                job.status = JobStatus.CANCELLED
                job.message = "任务在排队阶段被取消。"
            else:
                job.status = JobStatus.CANCEL_REQUESTED
                job.message = "已请求取消，等待当前阶段结束。"
            job.updated_at = utc_now()
        return job

    def count(self) -> int:
        with self._lock:
            return len(self._jobs)
