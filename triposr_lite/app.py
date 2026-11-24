"""FastAPI application for the TripoSR-Lite local workbench."""

from __future__ import annotations

import threading
import time
from urllib.parse import quote

from fastapi import FastAPI, File, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import registry, settings
from .adapter import TripoSRAdapter
from .imaging import ImageValidationError, estimate_foreground, validate_image_bytes
from .jobs import JobManager
from .meshes import (
    DEMO_LABEL,
    DEMO_NOTE,
    MeshExportError,
    MeshRecord,
    build_demo_mesh,
    export_glb,
    export_obj,
    mesh_payload,
)
from .store import MeshStore

PROBE_TTL_S = 8.0


def create_app(
    adapter: TripoSRAdapter | None = None,
    timeout_s: int | None = None,
) -> FastAPI:
    app = FastAPI(title=settings.APP_NAME, version=settings.VERSION, docs_url=None, redoc_url=None)

    adapter = adapter or TripoSRAdapter()
    store = MeshStore()
    demo = MeshRecord(
        id="demo",
        mesh=build_demo_mesh(),
        source="demo",
        label=DEMO_LABEL,
        note=DEMO_NOTE,
    )
    store.put(demo)
    manager = JobManager(adapter, store, demo, timeout_s=timeout_s)

    app.state.adapter = adapter
    app.state.store = store
    app.state.demo = demo
    app.state.manager = manager
    app.state.probe_cache = {"at": 0.0, "result": None, "lock": threading.Lock()}

    def current_probe():
        cache = app.state.probe_cache
        with cache["lock"]:
            if cache["result"] is not None and time.monotonic() - cache["at"] < PROBE_TTL_S:
                return cache["result"]
            result = adapter.probe()
            cache["result"] = result
            cache["at"] = time.monotonic()
            return result

    def job_payload(job):
        payload = {"job": job.as_dict(), "mesh": None}
        if job.mesh_id:
            record = store.get(job.mesh_id)
            if record is not None:
                payload["mesh"] = mesh_payload(record)
        return payload

    def error_response(status: int, code: str, message: str, detail: dict | None = None):
        return JSONResponse(status_code=status, content={"error": {"code": code, "message": message, "detail": detail or {}}})

    async def read_upload(file: UploadFile) -> bytes:
        limit = settings.max_image_bytes()
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise ImageValidationError(
                "IMAGE_TOO_LARGE",
                f"图片体积超过上限 {limit / 1048576:.0f} MB。",
                {"size_bytes": len(data), "limit_bytes": limit},
            )
        return data

    # ------------------------------------------------------------------ meta
    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "app": settings.APP_NAME,
            "version": settings.VERSION,
            "bind": f"{settings.DEFAULT_HOST}:{settings.DEFAULT_PORT}",
            "verification_status": registry.MODEL_CARD["verification_status"],
        }

    @app.get("/api/model")
    def model_info():
        probe = current_probe()
        return {"probe": probe.as_dict(), "model": registry.public_card()}

    @app.get("/api/demo")
    def demo_mesh():
        return {
            "demo": True,
            "label": DEMO_LABEL,
            "note": DEMO_NOTE,
            "mesh": mesh_payload(demo),
        }

    # ------------------------------------------------------------------ inspect
    @app.post("/api/inspect")
    async def inspect(file: UploadFile = File(...)):
        try:
            data = await read_upload(file)
            info = validate_image_bytes(data, filename=file.filename, declared_mime=file.content_type)
            hint = estimate_foreground(data)
            return {"image": info.as_dict(), "foreground": hint}
        except ImageValidationError as exc:
            return error_response(400, exc.code, exc.message, exc.detail)

    # ------------------------------------------------------------------ jobs
    @app.post("/api/jobs")
    async def create_job(file: UploadFile = File(...)):
        try:
            data = await read_upload(file)
            info = validate_image_bytes(data, filename=file.filename, declared_mime=file.content_type)
        except ImageValidationError as exc:
            return error_response(400, exc.code, exc.message, exc.detail)

        job = manager.submit(info, data)
        payload = job_payload(job)
        payload["probe"] = current_probe().as_dict()
        return payload

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return error_response(404, "JOB_NOT_FOUND", f"任务 {job_id} 不存在。")
        return job_payload(job)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return error_response(404, "JOB_NOT_FOUND", f"任务 {job_id} 不存在。")
        job = manager.cancel(job_id)
        return job_payload(job)

    @app.get("/api/jobs/{job_id}/mesh")
    def job_mesh(job_id: str):
        job = manager.get(job_id)
        if job is None:
            return error_response(404, "JOB_NOT_FOUND", f"任务 {job_id} 不存在。")
        if not job.mesh_id:
            return error_response(409, "MESH_NOT_AVAILABLE", f"任务 {job_id} 当前没有可预览网格（状态 {job.status.value}）。")
        record = store.get(job.mesh_id)
        if record is None:
            return error_response(410, "MESH_EVICTED", "网格已从内存中释放。")
        return {"demo": record.is_demo, "label": record.label, "mesh": mesh_payload(record)}

    # ------------------------------------------------------------------ export
    @app.get("/api/jobs/{job_id}/export")
    def export_mesh(job_id: str, format: str = Query(default="obj")):
        job = manager.get(job_id)
        if job is None:
            return error_response(404, "JOB_NOT_FOUND", f"任务 {job_id} 不存在。")
        if not job.mesh_id:
            return error_response(409, "MESH_NOT_AVAILABLE", f"任务状态 {job.status.value} 没有可导出网格。")
        record = store.get(job.mesh_id)
        if record is None:
            return error_response(410, "MESH_EVICTED", "网格已从内存中释放。")

        fmt = format.lower().strip()
        if fmt not in {"obj", "glb"}:
            return error_response(400, "INVALID_FORMAT", f"只支持 obj / glb，收到 {format}。")

        try:
            data = export_obj(record) if fmt == "obj" else export_glb(record)
        except MeshExportError as exc:
            return error_response(500, exc.code, exc.message, exc.detail)

        stem = "triposr_lite-demo" if record.is_demo else f"triposr_lite-{job.id}"
        filename = f"{stem}.{fmt}"
        media = "text/plain; charset=utf-8" if fmt == "obj" else "model/gltf-binary"
        headers = {
            "Content-Disposition": f"attachment; filename=\"{filename}\"; filename*=UTF-8''{quote(filename)}",
            "X-TripoSR-Demo": "true" if record.is_demo else "false",
            "X-TripoSR-Source": record.source,
            "Cache-Control": "no-store",
        }
        return Response(content=data, media_type=media, headers=headers)

    # ------------------------------------------------------------------ static UI
    @app.get("/")
    def index():
        return FileResponse(settings.WEB_DIR / "index.html", media_type="text/html; charset=utf-8")

    app.mount("/assets", StaticFiles(directory=settings.WEB_DIR), name="assets")
    return app


app = create_app()
