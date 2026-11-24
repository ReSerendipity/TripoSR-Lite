"""Readiness probe semantics: never report READY without weights + upstream stack."""

import pytest

import meshmint.adapter as adapter_mod
from meshmint.adapter import AdapterError, TripoSRAdapter
from meshmint.registry import MODEL_CARD, model_card


def _no_runtime(monkeypatch):
    monkeypatch.setattr(adapter_mod, "_module_available", lambda name: False)
    monkeypatch.setattr(
        adapter_mod,
        "_torch_details",
        lambda: {"available": False, "version": None, "cuda_available": False, "device_name": None},
    )


def test_probe_reports_all_missing_pieces(tmp_path, monkeypatch):
    _no_runtime(monkeypatch)
    adapter = TripoSRAdapter(weights_dir=tmp_path / "absent")
    probe = adapter.probe()

    assert probe.state == "NOT_READY"
    assert probe.ready is False
    codes = {reason["code"] for reason in probe.reasons}
    assert {"MISSING_CONFIG", "MISSING_WEIGHTS", "MISSING_TORCH", "MISSING_UPSTREAM_CODE"} <= codes
    details = probe.details
    assert details["weight"]["bytes"] == 0
    assert details["weights_dir"].endswith("absent")
    assert "stabilityai/TripoSR" in details["download_command"]
    assert details["official_vram_gb"] == 6


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
    monkeypatch.setattr(adapter_mod, "_module_available", lambda name: name in {"tsr", "torch"})
    monkeypatch.setattr(
        adapter_mod,
        "_torch_details",
        lambda: {"available": True, "version": "2.4.0+cu121", "cuda_available": True, "device_name": "RTX test"},
    )
    model_dir = tmp_path / "ready"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("cond_image_size: 512\n", encoding="utf-8")
    (model_dir / "model.ckpt").write_bytes(b"\0" * 2048)

    probe = TripoSRAdapter(weights_dir=model_dir, min_ckpt_bytes=1024).probe()
    assert probe.state == "READY"
    assert probe.reasons == []
    assert probe.warnings == []
    assert probe.details["torch"]["cuda_available"] is True


def test_cpu_only_is_a_warning_not_a_blocker(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter_mod, "_module_available", lambda name: name in {"tsr", "torch"})
    monkeypatch.setattr(
        adapter_mod,
        "_torch_details",
        lambda: {"available": True, "version": "2.4.0", "cuda_available": False, "device_name": None},
    )
    model_dir = tmp_path / "cpu"
    model_dir.mkdir()
    (model_dir / "config.yaml").write_text("cond_image_size: 512\n", encoding="utf-8")
    (model_dir / "model.ckpt").write_bytes(b"\0" * 2048)

    probe = TripoSRAdapter(weights_dir=model_dir, min_ckpt_bytes=1024).probe()
    assert probe.state == "READY"
    assert [warning["code"] for warning in probe.warnings] == ["NO_CUDA"]


def test_generate_refuses_when_not_ready(tmp_path, monkeypatch):
    _no_runtime(monkeypatch)
    adapter = TripoSRAdapter(weights_dir=tmp_path / "absent")
    with pytest.raises(AdapterError) as exc:
        adapter.generate(b"png-bytes")
    assert exc.value.code == "NOT_READY"
    assert "未执行推理" in exc.value.message


def test_registry_keeps_code_and_weight_licences_separate():
    card = model_card()
    assert card["code_license"] == "MIT"
    assert card["weights_license"] == "MIT"
    assert card["training_data"]["license"] == "CC-BY"
    assert card["auxiliary_weights"][0]["license"] == "Apache-2.0"
    assert all(item["redistributed"] is False for item in card["weight_files"])
    assert card["verification_status"] == "CODE_READY_MODEL_UNTESTED"
    assert card["official_hardware_claim"]["basis"] == "official-statement"
