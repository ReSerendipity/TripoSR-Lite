"""Job state machine: NOT_READY, DONE, CANCELLED, TIMEOUT and FAILED."""

import time

import pytest

from meshmint.adapter import AdapterError, GenerationCancelled, InferenceResult, ProbeResult, TripoSRAdapter
from meshmint.imaging import ImageInfo
from meshmint.jobs import JobManager, JobStatus
from meshmint.meshes import DEMO_NOTE, MeshRecord, build_demo_mesh
from meshmint.store import MeshStore


class ReadyAdapter:
    def probe(self) -> ProbeResult:
        return ProbeResult(state="READY", details={"weights_dir": "fake"})


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


def _manager(generator=None, timeout_s=5) -> JobManager:
    store = MeshStore()
    demo = MeshRecord(id="demo", mesh=build_demo_mesh(), source="demo", label="DEMO", note=DEMO_NOTE)
    store.put(demo)
    return JobManager(ReadyAdapter(), store, demo, timeout_s=timeout_s, generator=generator)


def _wait_terminal(manager: JobManager, job_id: str, timeout: float = 8.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job and job.status.terminal:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach a terminal state")


def test_not_ready_job_exposes_demo_mesh(tmp_path):
    store = MeshStore()
    demo = MeshRecord(id="demo", mesh=build_demo_mesh(), source="demo", label="DEMO", note=DEMO_NOTE)
    store.put(demo)
    adapter = TripoSRAdapter(weights_dir=tmp_path / "missing")
    manager = JobManager(adapter, store, demo)

    job = manager.submit(_image_info(), b"fake-bytes")

    assert job.status == JobStatus.NOT_READY
    assert job.demo is True
    assert job.mesh_id == "demo"
    assert job.error_code == "NOT_READY"
    assert "未执行 TripoSR 推理" in job.message
    assert job.as_dict()["export"]["obj"].endswith("format=obj")


def test_done_job_stores_a_model_mesh_and_timings():
    def generator(**kwargs):
        kwargs["on_stage"]("load_model", 0.05)
        kwargs["on_stage"]("forward", 0.4)
        return InferenceResult(mesh=build_demo_mesh(), device="cpu", timings={"forward": 0.02}, mc_resolution=256)

    manager = _manager(generator)
    job = manager.submit(_image_info(), b"fake-bytes")
    done = _wait_terminal(manager, job.id)

    assert done.status == JobStatus.DONE
    assert done.progress == 1.0
    assert done.demo is False
    assert done.duration_s is not None and done.duration_s >= 0
    assert done.timings["forward"] == 0.02
    record = manager.store.get(done.mesh_id)
    assert record is not None
    assert record.source == "model"
    assert record.label == "TripoSR"
    assert len(record.mesh.vertices) == 642


def test_cancel_moves_running_job_to_cancelled():
    def slow_generator(**kwargs):
        kwargs["on_stage"]("load_model", 0.05)
        for _ in range(200):
            if kwargs["should_cancel"]():
                raise GenerationCancelled("cancel during preprocess")
            time.sleep(0.01)
        raise AssertionError("cancel flag was never observed")

    manager = _manager(slow_generator)
    job = manager.submit(_image_info(), b"fake-bytes")

    deadline = time.monotonic() + 5
    while manager.get(job.id).status != JobStatus.RUNNING:
        assert time.monotonic() < deadline, "job never started"
        time.sleep(0.01)

    requested = manager.cancel(job.id)
    assert requested.status in {JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED}

    cancelled = _wait_terminal(manager, job.id)
    assert cancelled.status == JobStatus.CANCELLED
    assert cancelled.cancel_requested is True
    assert "阶段边界" in cancelled.message


def test_timeout_is_reported_and_result_is_discarded():
    def slow_then_success(**kwargs):
        time.sleep(0.25)
        return InferenceResult(mesh=build_demo_mesh(), device="cpu", timings={}, mc_resolution=256)

    manager = _manager(slow_then_success, timeout_s=0.1)
    job = manager.submit(_image_info(), b"fake-bytes")
    timed_out = _wait_terminal(manager, job.id)

    assert timed_out.status == JobStatus.TIMEOUT
    assert timed_out.error_code == "TIMEOUT"
    assert timed_out.mesh_id is None
    assert manager.store.get(f"job-{job.id}") is None


def test_adapter_error_becomes_failed_job():
    def failing(**kwargs):
        raise AdapterError("INFERENCE_FAILED", "torchmcubes 未编译 CUDA 扩展")

    manager = _manager(failing)
    job = manager.submit(_image_info(), b"fake-bytes")
    failed = _wait_terminal(manager, job.id)

    assert failed.status == JobStatus.FAILED
    assert failed.error_code == "INFERENCE_FAILED"
    assert "torchmcubes" in failed.message
    assert failed.mesh_id is None


def test_unexpected_exception_is_not_a_false_success():
    def exploding(**kwargs):
        raise RuntimeError("boom")

    manager = _manager(exploding)
    job = manager.submit(_image_info(), b"fake-bytes")
    failed = _wait_terminal(manager, job.id)

    assert failed.status == JobStatus.FAILED
    assert failed.error_code == "UNEXPECTED_ERROR"
    assert failed.mesh_id is None


def test_cancel_on_terminal_job_is_a_noop():
    def generator(**kwargs):
        return InferenceResult(mesh=build_demo_mesh(), device="cpu", timings={}, mc_resolution=256)

    manager = _manager(generator)
    job = manager.submit(_image_info(), b"fake-bytes")
    done = _wait_terminal(manager, job.id)
    assert done.status == JobStatus.DONE

    again = manager.cancel(job.id)
    assert again.status == JobStatus.DONE


def test_missing_job_returns_none():
    manager = _manager(lambda **kwargs: None)
    assert manager.get("does-not-exist") is None
    assert manager.cancel("does-not-exist") is None
