"""Adapter subprocess chain: argv, artifacts, success/failure, cancel and timeout."""

import json
import time
from pathlib import Path

import pytest

import triposr_lite.adapter as adapter_mod
from triposr_lite.adapter import GenerationCancelled, TripoSRAdapter
from triposr_lite.imaging import ImageInfo
from triposr_lite.jobs import JobManager, JobStatus
from triposr_lite.meshes import DEMO_NOTE, MeshRecord, build_demo_mesh
from triposr_lite.store import MeshStore

from helpers import build_ready_adapter, fake_supervisor_path, ok_runtime_probe, png_bytes


@pytest.fixture(autouse=True)
def _ready_runtime(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_external_runtime_probe", ok_runtime_probe)


def _image_info() -> ImageInfo:
    return ImageInfo(
        filename="test.png",
        declared_mime="image/png",
        width=256,
        height=256,
        format="PNG",
        mode="RGB",
        size_bytes=1234,
        has_alpha=False,
    )


def _manager(adapter: TripoSRAdapter, timeout_s: float = 10) -> JobManager:
    store = MeshStore()
    demo = MeshRecord(id="demo", mesh=build_demo_mesh(), source="demo", label="DEMO", note=DEMO_NOTE)
    store.put(demo)
    return JobManager(adapter, store, demo, timeout_s=timeout_s)


def _wait_terminal(manager: JobManager, job_id: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job and job.status.terminal:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def _artifact_dir(job) -> Path:
    path = Path(job.timings["artifact_dir"])
    assert path.is_dir()
    return path


def test_generate_builds_argv_and_isolated_output_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", "ok")
    adapter = build_ready_adapter(tmp_path)
    data = png_bytes()

    first = adapter.generate(data, "photo.PNG", mc_resolution=128, chunk_size=4096, remove_background=False)
    second = adapter.generate(data, "photo.PNG", mc_resolution=128, chunk_size=4096)

    dir_a = _artifact_dir_from_result(first)
    dir_b = _artifact_dir_from_result(second)
    assert dir_a != dir_b
    assert dir_a.parent == tmp_path / "results"
    assert dir_a.name.startswith("app-")

    request = json.loads((dir_a / "request.json").read_text(encoding="utf-8"))
    argv = request["argv"]
    assert argv[0] == str(Path(adapter.runtime_python()))
    assert argv[1] == str(fake_supervisor_path())
    assert argv[argv.index("--mc-resolution") + 1] == "128"
    assert argv[argv.index("--chunk-size") + 1] == "4096"
    assert argv[argv.index("--min-free-mib") + 1] == "512"
    assert argv[argv.index("--model-dir") + 1] == str(adapter.weights_dir())
    assert argv[argv.index("--device") + 1] == "cuda:0"
    assert "--no-remove-bg" in argv
    assert "--foreground-ratio" not in argv

    record = json.loads((dir_a / "argv.json").read_text(encoding="utf-8"))
    assert record["cwd"] == str(adapter_mod.settings.REPO_ROOT)
    assert record["unbuffered"] == "1"
    assert "scripts/_compat" not in (record["pythonpath"] or "")
    assert Path(record["image"]).read_bytes() == data

    second_argv = json.loads((dir_b / "argv.json").read_text(encoding="utf-8"))
    assert second_argv["remove_background"] is True
    assert second_argv["foreground_ratio"] == 0.85
    assert "--foreground-ratio" in json.loads((dir_b / "request.json").read_text(encoding="utf-8"))["argv"]


def _artifact_dir_from_result(result) -> Path:
    path = Path(result.timings["artifact_dir"])
    assert path.is_dir()
    return path


def test_cpu_only_probe_passes_cpu_device_to_supervisor(tmp_path, monkeypatch):
    def cpu_probe(*_args, **_kwargs):
        return {**ok_runtime_probe(), "cuda_available": False, "device_name": None}

    monkeypatch.setattr(adapter_mod, "_external_runtime_probe", cpu_probe)
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", "ok")
    adapter = build_ready_adapter(tmp_path)

    result = adapter.generate(png_bytes(), "photo.png")
    argv = json.loads((_artifact_dir_from_result(result) / "request.json").read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--device") + 1] == "cpu"


def test_fake_supervisor_glb_lands_in_store_as_done_job(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", "ok")
    manager = _manager(build_ready_adapter(tmp_path))
    job = manager.submit(_image_info(), png_bytes())
    done = _wait_terminal(manager, job.id)

    assert done.status == JobStatus.DONE
    assert done.demo is False
    assert done.mesh_id == f"job-{job.id}"
    record = manager.store.get(done.mesh_id)
    assert record is not None
    assert record.source == "model"
    assert len(record.mesh.vertices) == 162
    assert len(record.mesh.faces) == 320
    assert done.timings["device"] == "cpu"
    assert done.timings["status"] == "MODEL_TESTED"
    assert done.progress == 1.0
    assert _artifact_dir(done).joinpath("mesh.glb").is_file()
    assert _artifact_dir(done).joinpath("run.log").is_file()


def test_stage_progress_comes_from_run_log(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", "ok")
    adapter = build_ready_adapter(tmp_path)
    seen: list[tuple[str, float]] = []

    adapter.generate(png_bytes(), "photo.png", on_stage=lambda stage, progress: seen.append((stage, progress)))

    stages = [stage for stage, _ in seen]
    assert stages[0] == "load_model"
    for expected in ("load_model", "preprocess", "forward", "extract", "done"):
        assert expected in stages
    assert seen[-1] == ("done", 1.0)
    assert all(0.0 <= progress <= 1.0 for _, progress in seen)


@pytest.mark.parametrize(
    ("mode", "code", "expects_metrics"),
    [
        ("fail", "SUPERVISOR_FAILED", True),
        ("crash", "SUPERVISOR_FAILED", False),
        ("empty_mesh", "EMPTY_MESH", True),
    ],
)
def test_failed_supervisor_keeps_evidence(tmp_path, monkeypatch, mode, code, expects_metrics):
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", mode)
    manager = _manager(build_ready_adapter(tmp_path))
    job = manager.submit(_image_info(), png_bytes())
    failed = _wait_terminal(manager, job.id)

    assert failed.status == JobStatus.FAILED
    assert failed.error_code == code
    assert failed.mesh_id is None
    assert manager.store.get(f"job-{job.id}") is None

    artifact = Path(failed.error_detail["detail"]["artifact_dir"])
    assert artifact.is_dir()
    assert (artifact / "request.json").is_file()
    assert (artifact / "supervisor.log").is_file()
    assert (artifact / "metrics.json").is_file() is expects_metrics


@pytest.mark.parametrize(
    ("mode", "code"),
    [("gpu_busy", "GPU_BUSY"), ("low_vram", "INSUFFICIENT_VRAM")],
)
def test_gpu_gate_is_a_hard_failure_without_fallback(tmp_path, monkeypatch, mode, code):
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", mode)
    manager = _manager(build_ready_adapter(tmp_path))
    job = manager.submit(_image_info(), png_bytes())
    failed = _wait_terminal(manager, job.id)

    assert failed.status == JobStatus.FAILED
    assert failed.error_code == code
    assert failed.mesh_id is None
    assert failed.demo is False
    assert "export" not in failed.as_dict()
    assert manager.store.get(f"job-{job.id}") is None
    artifact = Path(failed.error_detail["detail"]["artifact_dir"])
    metrics = json.loads((artifact / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["status"] == "BLOCKED"
    assert failed.error_detail["detail"]["exit_code"] == 3
    assert failed.error_detail["detail"]["status"] == "BLOCKED"
    assert (artifact / "request.json").is_file()
    assert (artifact / "supervisor.log").is_file()
    assert not (artifact / "mesh.glb").exists()
    assert not (artifact / "mesh.obj").exists()
    assert not (artifact / "termination.json").exists(), "hard gate failure is not a cancel/timeout"


def test_cancel_terminates_supervisor_and_discards_result(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", "sleep")
    manager = _manager(build_ready_adapter(tmp_path), timeout_s=30)
    job = manager.submit(_image_info(), png_bytes())

    deadline = time.monotonic() + 8
    while manager.get(job.id).status != JobStatus.RUNNING:
        assert time.monotonic() < deadline, "job never started"
        time.sleep(0.02)
    # Wait until the supervisor is really running before cancelling, so this
    # exercises tree termination rather than the pre-launch cancel path.
    while not list((tmp_path / "results").glob("app-*/run.log")):
        assert time.monotonic() < deadline, "supervisor never started"
        time.sleep(0.05)

    manager.cancel(job.id)
    cancelled = _wait_terminal(manager, job.id, timeout=10)

    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert cancelled.mesh_id is None
    assert "已取消" in cancelled.message
    artifacts = list((tmp_path / "results").glob("app-*"))
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert (artifact / "request.json").is_file()
    assert (artifact / "termination.json").is_file()
    assert (artifact / "run.log").is_file()
    termination = json.loads((artifact / "termination.json").read_text(encoding="utf-8"))
    assert termination["reason"] == "GenerationCancelled"
    assert termination["supervisor_started"] is True
    assert isinstance(termination.get("pid"), int)
    assert not (artifact / "mesh.glb").exists()
    assert not (artifact / "mesh.obj").exists()
    time.sleep(1.0)
    assert not (artifact / "finished.marker").exists(), "supervisor outlived the cancel"
    assert manager.store.get(f"job-{job.id}") is None


def test_timeout_terminates_supervisor_and_discards_result(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_SUPERVISOR_MODE", "sleep")
    manager = _manager(build_ready_adapter(tmp_path), timeout_s=1)
    job = manager.submit(_image_info(), png_bytes())
    timed_out = _wait_terminal(manager, job.id, timeout=15)

    assert timed_out.status == JobStatus.TIMEOUT
    assert timed_out.error_code == "TIMEOUT"
    assert timed_out.mesh_id is None
    assert manager.store.get(f"job-{job.id}") is None

    artifacts = list((tmp_path / "results").glob("app-*"))
    assert len(artifacts) == 1
    assert (artifacts[0] / "termination.json").is_file()
    termination = json.loads((artifacts[0] / "termination.json").read_text(encoding="utf-8"))
    assert termination["reason"] == "GenerationTimeout"
    assert termination["supervisor_started"] is True
    assert isinstance(termination.get("pid"), int)
    assert not (artifacts[0] / "mesh.glb").exists()
    assert not (artifacts[0] / "mesh.obj").exists()
    time.sleep(1.0)
    assert not (artifacts[0] / "finished.marker").exists(), "supervisor outlived the timeout"


def test_cancel_before_supervisor_launch_still_writes_termination(tmp_path):
    adapter = build_ready_adapter(tmp_path)

    with pytest.raises(GenerationCancelled):
        adapter.generate(png_bytes(), "photo.png", should_cancel=lambda: True)

    artifacts = list((tmp_path / "results").glob("app-*"))
    assert len(artifacts) == 1
    artifact = artifacts[0]
    termination = json.loads((artifact / "termination.json").read_text(encoding="utf-8"))
    assert termination["reason"] == "GenerationCancelled"
    assert termination["supervisor_started"] is False
    assert (artifact / "request.json").is_file()
    assert not (artifact / "supervisor.log").exists()
    assert not (artifact / "mesh.glb").exists()
    assert not (artifact / "mesh.obj").exists()
