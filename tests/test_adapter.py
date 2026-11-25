"""Readiness probe semantics: never report READY without weights + external runtime."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

import triposr_lite.adapter as adapter_mod
from triposr_lite.adapter import TripoSRAdapter, _probe_runtime_process
from triposr_lite.registry import MODEL_CARD, model_card

from helpers import ok_runtime_probe


def _no_runtime(monkeypatch, code="MISSING_TORCH"):
    monkeypatch.setattr(
        adapter_mod,
        "_external_runtime_probe",
        lambda *args, **kwargs: {"ok": False, "code": code, "error": f"stub {code}"},
    )


def test_probe_reports_all_missing_pieces(tmp_path, monkeypatch):
    _no_runtime(monkeypatch)
    adapter = TripoSRAdapter(weights_dir=tmp_path / "absent")
    probe = adapter.probe()

    assert probe.state == "NOT_READY"
    assert probe.ready is False
    codes = {reason["code"] for reason in probe.reasons}
    assert {"MISSING_CONFIG", "MISSING_WEIGHTS", "MISSING_TORCH"} <= codes
    details = probe.details
    assert details["weight"]["bytes"] == 0
    assert details["weights_dir"].endswith("absent")
    assert "stabilityai/TripoSR" in details["download_command"]
    assert details["official_vram_gb"] == 6
    assert details["app_process_torch_imported"] is False


def test_missing_upstream_and_supervisor_are_upstream_errors(tmp_path, monkeypatch):
    _no_runtime(monkeypatch)
    adapter = TripoSRAdapter(
        weights_dir=tmp_path / "weights",
        upstream_dir=tmp_path / "no-upstream",
        supervisor=tmp_path / "no-supervisor.py",
    )
    probe = adapter.probe()
    messages = [reason["message"] for reason in probe.reasons if reason["code"] == "MISSING_UPSTREAM_CODE"]
    assert len(messages) == 2
    assert probe.details["upstream"]["present"] is False
    assert probe.details["supervisor"]["present"] is False


def test_truncated_checkpoint_is_flagged(tmp_path, monkeypatch):
    _no_runtime(monkeypatch)
    model_dir = tmp_path / "models"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("cond_image_size: 512\n", encoding="utf-8")
    (model_dir / "model.ckpt").write_bytes(b"\0" * (5 * 1024 * 1024))

    probe = TripoSRAdapter(weights_dir=model_dir).probe()
    codes = {reason["code"] for reason in probe.reasons}
    assert "WEIGHT_TOO_SMALL" in codes
    assert "MISSING_WEIGHTS" not in codes
    assert probe.details["weight"]["bytes"] == 5 * 1024 * 1024
    assert probe.details["weight"]["min_bytes"] == MODEL_CARD["min_ckpt_bytes"]


def test_probe_ready_only_when_stack_is_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "_external_runtime_probe", ok_runtime_probe)
    from helpers import build_ready_adapter

    adapter = build_ready_adapter(tmp_path)
    probe = adapter.probe()

    assert probe.state == "READY"
    assert probe.reasons == []
    assert probe.warnings == []
    runtime = probe.details["runtime"]
    assert runtime["probe"]["cuda_available"] is True
    assert runtime["present"] is True
    assert Path(probe.details["weight"]["path"]).parent == adapter.weights_dir()


def test_cpu_only_is_a_warning_not_a_blocker(tmp_path, monkeypatch):
    def cpu_probe(*_args, **_kwargs):
        return {**ok_runtime_probe(), "cuda_available": False, "device_name": None}

    monkeypatch.setattr(adapter_mod, "_external_runtime_probe", cpu_probe)
    from helpers import build_ready_adapter

    probe = build_ready_adapter(tmp_path).probe()
    assert probe.state == "READY"
    assert [warning["code"] for warning in probe.warnings] == ["NO_CUDA"]


@pytest.mark.parametrize("probe_code", ["RUNTIME_PROBE_FAILED", "MISSING_UPSTREAM_CODE", "MISSING_TORCH"])
def test_runtime_probe_failures_are_reasons(tmp_path, monkeypatch, probe_code):
    _no_runtime(monkeypatch, probe_code)
    from helpers import build_ready_adapter

    probe = build_ready_adapter(tmp_path).probe()
    assert probe.state == "NOT_READY"
    assert [reason["code"] for reason in probe.reasons] == [probe_code]


def test_generate_refuses_when_not_ready(tmp_path, monkeypatch):
    _no_runtime(monkeypatch)
    adapter = TripoSRAdapter(weights_dir=tmp_path / "absent")
    with pytest.raises(adapter_mod.AdapterError) as exc:
        adapter.generate(b"png-bytes")
    assert exc.value.code == "NOT_READY"
    assert "未执行推理" in exc.value.message


def test_probe_runtime_process_reports_missing_python(tmp_path):
    result = _probe_runtime_process(tmp_path / "absent-python.exe", tmp_path, 2.0)
    assert result["ok"] is False
    assert result["code"] == "MISSING_TORCH"


@pytest.mark.skipif(
    importlib.util.find_spec("torch") is not None,
    reason="app interpreter has torch; the missing-torch path cannot be exercised",
)
def test_probe_runtime_process_classifies_app_python_without_torch(tmp_path):
    result = _probe_runtime_process(Path(sys.executable), tmp_path, 30.0)
    assert result["ok"] is False
    assert result["code"] == "MISSING_TORCH"
    assert result["exit_code"] == 3


def test_probe_runtime_process_timeout(monkeypatch, tmp_path):
    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="python", timeout=1)

    monkeypatch.setattr(adapter_mod.subprocess, "run", boom)
    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    result = _probe_runtime_process(python, tmp_path, 1.0)
    assert result["ok"] is False
    assert result["code"] == "RUNTIME_PROBE_FAILED"
    assert "超时" in result["error"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ModuleNotFoundError: No module named 'torch'", "MISSING_TORCH"),
        ("PROBE_FAILED: MISSING_TORCH", "MISSING_TORCH"),
        ("ModuleNotFoundError: No module named 'tsr'", "MISSING_UPSTREAM_CODE"),
        ("ModuleNotFoundError: No module named 'torchmcubes'", "MISSING_UPSTREAM_CODE"),
        ("PROBE_FAILED: MISSING_UPSTREAM_CODE", "MISSING_UPSTREAM_CODE"),
        ("Segmentation fault", "RUNTIME_PROBE_FAILED"),
    ],
)
def test_classify_probe_failure(text, expected):
    assert adapter_mod.classify_probe_failure(text) == expected


def test_stage_updates_only_advance_on_real_log_markers():
    assert adapter_mod.stage_updates("", 0) == (0, [])
    log = "INFO - Initializing model ...\nINFO - Running model ...\n"
    reached, updates = adapter_mod.stage_updates(log, 0)
    assert reached == 3
    assert [stage for stage, _ in updates] == ["load_model", "preprocess", "forward"]
    assert adapter_mod.stage_updates(log, reached) == (3, [])


def test_registry_keeps_code_and_weight_licences_separate():
    card = model_card()
    assert card["code_license"] == "MIT"
    assert card["weights_license"] == "MIT"
    assert card["training_data"]["license"] == "CC-BY"
    assert card["auxiliary_weights"][0]["license"] == "Apache-2.0"
    assert all(item["redistributed"] is False for item in card["weight_files"])
    assert card["verification_status"] == "CODE_READY_MODEL_UNTESTED"
    assert card["official_hardware_claim"]["basis"] == "official-statement"
