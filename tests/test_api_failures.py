"""HTTP failure branches: GPU gate, cancel and timeout keep evidence and block export."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import triposr_lite.adapter as adapter_mod
from triposr_lite.app import create_app

from helpers import build_ready_adapter, ok_runtime_probe, png_bytes


def _make_client(tmp_path, monkeypatch, mode: str, timeout_s: int = 10) -> TestClient:
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", mode)
    monkeypatch.setattr(adapter_mod, "_external_runtime_probe", ok_runtime_probe)
    adapter = build_ready_adapter(tmp_path)
    return TestClient(create_app(adapter=adapter, timeout_s=timeout_s))


def _submit(client: TestClient) -> str:
    response = client.post(
        "/api/jobs",
        files={"file": ("test.png", png_bytes(), "image/png")},
    )
    assert response.status_code == 200
    return response.json()["job"]["id"]


def _wait_terminal(client: TestClient, job_id: str, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["terminal"]:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def _artifact(job: dict) -> Path:
    path = Path(job["error_detail"]["detail"]["artifact_dir"])
    assert path.is_dir()
    return path


def _assert_export_blocked(client: TestClient, job_id: str) -> None:
    mesh = client.get(f"/api/jobs/{job_id}/mesh")
    assert mesh.status_code == 409
    assert mesh.json()["error"]["code"] == "MESH_NOT_AVAILABLE"
    for fmt in ("obj", "glb"):
        response = client.get(f"/api/jobs/{job_id}/export?format={fmt}")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "MESH_NOT_AVAILABLE"


@pytest.mark.parametrize(
    ("mode", "code"),
    [("gpu_busy", "GPU_BUSY"), ("low_vram", "INSUFFICIENT_VRAM")],
)
def test_api_gpu_gate_is_failed_with_409_and_no_mesh(tmp_path, monkeypatch, mode, code):
    with _make_client(tmp_path, monkeypatch, mode) as client:
        job_id = _submit(client)
        job = _wait_terminal(client, job_id)

        assert job["status"] == "FAILED"
        assert job["error_code"] == code
        assert job["mesh_id"] is None
        assert "export" not in job
        detail = job["error_detail"]["detail"]
        assert detail["status"] == "BLOCKED"
        assert detail["exit_code"] == 3
        _assert_export_blocked(client, job_id)

        artifact = _artifact(job)
        metrics = json.loads((artifact / "metrics.json").read_text(encoding="utf-8"))
        assert metrics["status"] == "BLOCKED"
        assert (artifact / "request.json").is_file()
        assert (artifact / "supervisor.log").is_file()
        assert not (artifact / "mesh.glb").exists()
        assert not (artifact / "mesh.obj").exists()
        assert not (artifact / "termination.json").exists()


def test_api_cancel_writes_termination_and_blocks_export(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, "sleep", timeout_s=30) as client:
        job_id = _submit(client)

        deadline = time.monotonic() + 8
        while client.get(f"/api/jobs/{job_id}").json()["job"]["status"] != "RUNNING":
            assert time.monotonic() < deadline, "job never started"
            time.sleep(0.02)
        while not list((tmp_path / "results").glob("app-*/run.log")):
            assert time.monotonic() < deadline, "supervisor never started"
            time.sleep(0.05)

        cancel = client.post(f"/api/jobs/{job_id}/cancel")
        assert cancel.status_code == 200
        job = _wait_terminal(client, job_id, timeout=10)

        assert job["status"] == "CANCELLED"
        assert job["cancel_requested"] is True
        assert job["mesh_id"] is None
        _assert_export_blocked(client, job_id)

        artifact = next((tmp_path / "results").glob("app-*"))
        termination = json.loads((artifact / "termination.json").read_text(encoding="utf-8"))
        assert termination["reason"] == "GenerationCancelled"
        assert termination["supervisor_started"] is True
        assert not (artifact / "mesh.glb").exists()
        assert not (artifact / "mesh.obj").exists()


def test_api_timeout_writes_termination_and_blocks_export(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch, "sleep", timeout_s=1) as client:
        job_id = _submit(client)
        job = _wait_terminal(client, job_id, timeout=15)

        assert job["status"] == "TIMEOUT"
        assert job["error_code"] == "TIMEOUT"
        assert job["mesh_id"] is None
        _assert_export_blocked(client, job_id)

        artifact = next((tmp_path / "results").glob("app-*"))
        termination = json.loads((artifact / "termination.json").read_text(encoding="utf-8"))
        assert termination["reason"] == "GenerationTimeout"
        assert termination["supervisor_started"] is True
        assert not (artifact / "mesh.glb").exists()
        assert not (artifact / "mesh.obj").exists()
