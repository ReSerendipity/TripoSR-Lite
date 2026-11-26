"""S2 CLI 契约测试：Result 信封、退出码、--json 任意位置、stdout 隔离。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
CLI = ROOT / "app" / "integrated_app" / "cli.py"


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _envelope(proc: subprocess.CompletedProcess[str]) -> dict:
    return json.loads(proc.stdout.strip())


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("NOT_READY", 3),
        ("MISSING_WEIGHTS", 3),
        ("GPU_BUSY", 3),
        ("INSUFFICIENT_VRAM", 3),
        ("RUNTIME_PROBE_FAILED", 3),
        ("USAGE_ERROR", 2),
        ("INVALID_FORMAT", 2),
        ("JOB_NOT_FOUND", 2),
        ("IMAGE_UNREADABLE", 2),
        ("EMPTY_MESH", 1),
        ("SUPERVISOR_FAILED", 1),
        ("BUSINESS_ERROR", 1),
    ],
)
def test_exit_code_mapping(code: str, expected: int) -> None:
    from app.integrated_app.cli_output import Result

    result = Result.failure("x", code)
    assert result.exit_code() == expected
    assert result.to_dict() == {
        "ok": False,
        "command": "",
        "error": "x",
        "error_code": code,
    }


def test_json_flag_anywhere_and_envelope_shape() -> None:
    for argv in (["probe", "--json"], ["--json", "probe"], ["probe"] + ["--json"]):
        proc = _run_cli(*argv)
        assert proc.returncode == 0, proc.stderr
        payload = _envelope(proc)
        assert payload["ok"] is True
        assert payload["command"] == "probe"
        assert set(payload["data"]) >= {"app", "version", "state", "reasons", "model"}


def test_probe_is_result_envelope_not_bare_payload() -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "triposr_lite", "probe", "--json"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert _envelope(proc)["ok"] is True


def test_usage_error_exit_code() -> None:
    proc = _run_cli("inspect", "--json")
    assert proc.returncode == 2
    payload = _envelope(proc)
    assert payload["ok"] is False
    assert payload["error_code"] == "USAGE_ERROR"


def test_unknown_command_exit_code() -> None:
    proc = _run_cli("no-such-command", "--json")
    assert proc.returncode == 2
    assert "error" in (proc.stderr or "")


def test_json_mode_stdout_has_only_envelope(monkeypatch, capsys) -> None:
    from app.integrated_app import cli

    def noisy_probe() -> dict:
        print("IMPORT_NOISE_MUST_NOT_POLLUTE_JSON")
        return {"state": "READY"}

    monkeypatch.setattr(cli, "probe_payload", noisy_probe)
    code = cli.main(["probe", "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.count("\n") == 1
    assert json.loads(captured.out)["data"]["state"] == "READY"
    assert "IMPORT_NOISE_MUST_NOT_POLLUTE_JSON" in captured.err


def test_inspect_command(tmp_path: Path) -> None:
    image = tmp_path / "sample.png"
    Image.new("RGBA", (96, 96), (255, 0, 0, 255)).save(image)
    proc = _run_cli("inspect", "-i", str(image), "--json")
    assert proc.returncode == 0, proc.stderr
    data = _envelope(proc)["data"]
    assert data["image"]["width"] == 96
    assert "foreground_ratio" in data["foreground"]
    assert "mask_data_url" not in data["foreground"]


def test_inspect_bad_input_maps_usage_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"not an image")
    proc = _run_cli("inspect", "-i", str(bad), "--json")
    assert proc.returncode == 2
    assert _envelope(proc)["error_code"] == "IMAGE_UNREADABLE"


def test_model_download_is_dry_run_by_default() -> None:
    proc = _run_cli("model", "download", "--json")
    assert proc.returncode == 0, proc.stderr
    data = _envelope(proc)["data"]
    assert data["executed"] is False
    assert "huggingface-cli download" in data["command"]


def test_engines_and_models_lists() -> None:
    engines = _envelope(_run_cli("engines", "list", "--json"))
    models = _envelope(_run_cli("models", "list", "--json"))
    assert [engine["id"] for engine in engines["data"]["engines"]] == ["triposr"]
    assert models["data"]["files"][1]["filename"] == "config.yaml"


def test_config_redaction_function() -> None:
    from app.integrated_app.cli_service import _redact

    payload = _redact({"api_key": "x", "nest": {"token": "y", "safe": "z"}})
    assert payload["api_key"] == "***REDACTED***"
    assert payload["nest"]["token"] == "***REDACTED***"
    assert payload["nest"]["safe"] == "z"


def test_serve_rejects_non_loopback() -> None:
    from app.integrated_app.cli_service import CliServiceError, serve

    with pytest.raises(CliServiceError) as exc:
        serve(host="0.0.0.0", port=7861, timeout_s=10)
    assert exc.value.code == "USAGE_ERROR"


@pytest.mark.skipif(
    os.environ.get("TRIPOSR_RUN_REAL") != "1",
    reason="真实推理需外部运行时与权重；手动以 TRIPOSR_RUN_REAL=1 启用",
)
def test_real_generate_smoke() -> None:
    image = ROOT / "results" / "app-20261125-195357-b30f8bd5" / "input.png"
    proc = _run_cli("generate", "-i", str(image), "--json")
    assert proc.returncode == 0, proc.stderr
    data = _envelope(proc)["data"]
    assert Path(data["glb"]).is_file()
    assert Path(data["obj"]).is_file()
