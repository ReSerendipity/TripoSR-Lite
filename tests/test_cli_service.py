"""S2 服务层测试：任务证据目录、导出、未就绪 0 秒拦截、批量与批次恢复。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from app.integrated_app import cli_service
from app.integrated_app.cli_service import CliServiceError
from triposr_lite.adapter import ProbeResult
from triposr_lite.meshes import MeshRecord, build_demo_mesh, export_glb


@pytest.fixture
def results_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "results"
    root.mkdir()
    monkeypatch.setenv("TRIPOSR_RESULTS_DIR", str(root))
    monkeypatch.delenv("TRIPOSR_CLI_RESULTS_DIR", raising=False)
    return root


def _artifact(root: Path, name: str = "app-20261126-120000-abcdef12") -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True)
    record = MeshRecord(
        id=name,
        mesh=build_demo_mesh(),
        source="model",
        label="TripoSR",
        note="测试网格",
    )
    (path / "mesh.glb").write_bytes(export_glb(record))
    return path


class FakeAdapter:
    def __init__(self, artifact: Path, ready: bool = True) -> None:
        self.artifact = artifact
        self.ready = ready
        self.calls = 0

    def probe(self) -> ProbeResult:
        if self.ready:
            return ProbeResult(state="READY")
        return ProbeResult(
            state="NOT_READY",
            reasons=[{"code": "MISSING_WEIGHTS", "message": "缺少权重", "hint": "下载权重"}],
        )

    def generate(self, image_bytes: bytes, filename: str = "upload.png", **kwargs) -> object:
        self.calls += 1
        on_stage = kwargs.get("on_stage")
        if on_stage:
            on_stage("load_model", 0.25)
        record = MeshRecord(
            id=self.artifact.name,
            mesh=build_demo_mesh(),
            source="model",
            label="TripoSR",
            note="测试生成",
        )
        (self.artifact / "mesh.glb").write_bytes(export_glb(record))
        from triposr_lite.adapter import InferenceResult

        return InferenceResult(
            mesh=record.mesh,
            device="cuda:0",
            timings={"artifact_dir": str(self.artifact), "wall_seconds": 1.25, "status": "MODEL_TESTED"},
            mc_resolution=256,
        )


def test_jobs_list_show_and_mesh_export(results_root: Path) -> None:
    artifact = _artifact(results_root)
    (artifact / "metrics.json").write_text(
        json.dumps({"status": "MODEL_TESTED", "wall_seconds": 12.5}), encoding="utf-8"
    )
    (artifact / "process.json").write_text(json.dumps({"pid": 999999}), encoding="utf-8")

    listing = cli_service.jobs_list()
    assert listing["count"] == 1
    job = listing["jobs"][0]
    assert job["status"] == "DONE"
    assert job["has_mesh"] is True

    shown = cli_service.jobs_show(artifact.name)
    assert shown["mesh"]["vertices"] > 0
    assert shown["status"] == "DONE"

    stats = cli_service.mesh_show(artifact.name)
    assert stats["job_id"] == artifact.name
    assert stats["faces"] > 0

    exported = cli_service.mesh_export(artifact.name, "obj")
    assert Path(exported["path"]).is_file()
    assert exported["format"] == "obj"
    assert len(exported["sha256"]) == 64

    with pytest.raises(CliServiceError) as exc:
        cli_service.mesh_export(artifact.name, "stl")
    assert exc.value.code == "INVALID_FORMAT"


def test_jobs_cancel_writes_evidence_without_killing_runner(results_root: Path) -> None:
    artifact = _artifact(results_root, "app-20261126-120001-abcdef13")
    (artifact / "process.json").write_text(json.dumps({"pid": 999999}), encoding="utf-8")
    result = cli_service.jobs_cancel(artifact.name)
    assert result["status"] == "CANCELLED"
    assert result["terminated"] is False  # 死 pid，不误杀任何进程
    assert (artifact / "cancellation.json").is_file()

    done = _artifact(results_root, "app-20261126-120002-abcdef14")
    (done / "metrics.json").write_text(json.dumps({"status": "MODEL_TESTED"}), encoding="utf-8")
    with pytest.raises(CliServiceError) as exc:
        cli_service.jobs_cancel(done.name)
    assert exc.value.code == "BUSINESS_ERROR"


def test_generate_writes_fixed_outputs(results_root: Path, tmp_path: Path, monkeypatch) -> None:
    artifact = _artifact(results_root)
    fake = FakeAdapter(artifact)
    monkeypatch.setattr(cli_service, "adapter", lambda: fake)
    image = tmp_path / "input.png"
    Image.new("RGB", (80, 80), (20, 120, 200)).save(image)

    stages: list[str] = []
    data = cli_service.generate_image(str(image), progress_cb=lambda stage, _: stages.append(stage))

    assert fake.calls == 1
    assert stages  # 阶段回调确实透传
    assert data["job_id"] == artifact.name
    assert Path(data["obj"]).is_file() and Path(data["glb"]).is_file()
    summary = json.loads((cli_service.cli_results_dir() / artifact.name / "summary.json").read_text(encoding="utf-8"))
    assert summary["stats"]["faces"] > 0


def test_generate_not_ready_is_blocked_before_subprocess(results_root: Path, tmp_path: Path, monkeypatch) -> None:
    artifact = _artifact(results_root, "app-20261126-120003-abcdef15")
    fake = FakeAdapter(artifact, ready=False)
    monkeypatch.setattr(cli_service, "adapter", lambda: fake)
    image = tmp_path / "input.png"
    Image.new("RGB", (80, 80), (20, 120, 200)).save(image)

    with pytest.raises(CliServiceError) as exc:
        cli_service.generate_image(str(image))
    assert exc.value.code == "MISSING_WEIGHTS"
    assert exc.value.hint == "下载权重"
    assert fake.calls == 0  # 0 秒拦截：没起监督子进程


def test_batch_resume_uses_recorded_failures(results_root: Path, tmp_path: Path, monkeypatch) -> None:
    artifact = _artifact(results_root)
    fake = FakeAdapter(artifact)
    monkeypatch.setattr(cli_service, "adapter", lambda: fake)
    good = tmp_path / "good.png"
    Image.new("RGB", (64, 64), (1, 2, 3)).save(good)

    data = cli_service.batch_generate([str(good), str(tmp_path / "missing.png")])
    assert data["succeeded"] == 1
    assert data["failed"] == 1
    batch_path = cli_service.cli_results_dir() / f"{data['batch_id']}.json"
    assert batch_path.is_file()

    # 模拟用户补上了此前失败的输入文件，再恢复批次。
    recovered = tmp_path / "missing.png"
    Image.new("RGB", (64, 64), (9, 9, 9)).save(recovered)
    resumed = cli_service.batch_resume(data["batch_id"])
    assert resumed["resumed"] == 1
    assert resumed["resumed_from"] == data["batch_id"]
