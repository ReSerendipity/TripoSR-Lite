"""S3 契约测试：端口文件、服务发现、HTTP 提交/下载、六种降级回落直调。"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from PIL import Image

from app.integrated_app import cli, cli_remote
from app.integrated_app.cli_remote import RemoteError
from triposr_lite import server_port


def test_port_file_roundtrip_and_env_override(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / ".server_port"
    monkeypatch.setenv("TRIPOSR_PORT_FILE", str(path))
    assert server_port.port_file_path() == path
    assert server_port.read_port_file() is None
    server_port.write_port_file(54321)
    assert path.read_text(encoding="ascii") == "54321"
    assert server_port.read_port_file() == 54321
    path.write_text("garbage", encoding="ascii")
    assert server_port.read_port_file() is None
    server_port.write_port_file(1)
    assert server_port.read_port_file() is None  # 非法端口
    server_port.write_port_file(54321)
    server_port.clear_port_file()
    assert server_port.read_port_file() is None


def test_discover_server_priority(tmp_path: Path, monkeypatch) -> None:
    port_file = tmp_path / ".server_port"
    port_file.write_text("43210", encoding="ascii")
    monkeypatch.setenv("TRIPOSR_PORT_FILE", str(port_file))
    monkeypatch.setenv("TRIPOSR_CLI_SERVER", "http://127.0.0.1:9998")
    seen: list[str] = []

    def fake_probe(base: str, timeout: float = 2.0, transport=None) -> dict:
        seen.append(base)
        if base == "http://127.0.0.1:9997":
            return {"status": "ok"}
        raise RemoteError("unreachable")

    monkeypatch.setattr(cli_remote, "probe", fake_probe)
    assert cli_remote.discover_server("http://127.0.0.1:9997") == "http://127.0.0.1:9997"
    assert seen == ["http://127.0.0.1:9997"]

    seen.clear()
    monkeypatch.setattr(cli_remote, "probe", lambda base, timeout=2.0, transport=None: {"status": "ok"})
    assert cli_remote.discover_server(None) == "http://127.0.0.1:9998"
    seen.clear()

    monkeypatch.delenv("TRIPOSR_CLI_SERVER")
    assert cli_remote.discover_server(None) == "http://127.0.0.1:43210"


class FakeService:
    """httpx MockTransport：模拟真实 /api/* JSON 契约与文件流。"""

    def __init__(self) -> None:
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"status": "ok", "app": "TripoSR-Lite", "version": "0.1.0"})
        if path == "/api/inspect":
            return httpx.Response(
                200,
                json={"image": {"width": 64}, "foreground": {"foreground_ratio": 0.5, "mask_data_url": "x"}},
            )
        if path == "/api/jobs" and request.method == "POST":
            return httpx.Response(
                200,
                json={"job": {"id": "job-remote-1", "status": "QUEUED", "terminal": False, "progress": 0.0}},
            )
        if path == "/api/jobs/job-remote-1":
            self.polls += 1
            done = self.polls >= 2
            return httpx.Response(
                200,
                json={
                    "job": {
                        "id": "job-remote-1",
                        "status": "DONE" if done else "RUNNING",
                        "terminal": done,
                        "stage": "forward" if not done else "done",
                        "progress": 0.7 if not done else 1.0,
                        "timings": {"device": "cuda:0"},
                    }
                },
            )
        if path == "/api/jobs/job-remote-1/mesh":
            return httpx.Response(200, json={"mesh": {"stats": {"vertices": 12, "faces": 20}}})
        if path == "/api/jobs/job-remote-1/export":
            return httpx.Response(200, content=b"fake mesh bytes")
        return httpx.Response(404, json={"error": "missing"})


@pytest.fixture
def fake_service(monkeypatch) -> FakeService:
    service = FakeService()
    original = cli_remote._client

    def factory(base: str, timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
        return original(base, timeout, httpx.MockTransport(service.handler))

    monkeypatch.setattr(cli_remote, "_client", factory)
    return service


def test_remote_inspect_and_generate(tmp_path: Path, fake_service: FakeService) -> None:
    image = tmp_path / "input.png"
    Image.new("RGB", (64, 64), (5, 6, 7)).save(image)

    inspected = cli_remote.inspect("http://127.0.0.1:9999", str(image))
    assert inspected["image"]["width"] == 64
    assert "mask_data_url" not in inspected["foreground"]

    stages: list[str] = []
    data = cli_remote.generate(
        "http://127.0.0.1:9999",
        str(image),
        output_dir=str(tmp_path / "out"),
        progress_cb=lambda stage, _: stages.append(stage),
    )
    assert data["mode"] == "remote"
    assert data["job_id"] == "job-remote-1"
    assert data["stats"]["faces"] == 20
    assert Path(data["obj"]).read_bytes() == b"fake mesh bytes"
    assert Path(data["glb"]).is_file()
    assert "forward" in stages and "done" in stages
    assert fake_service.polls >= 2


def test_remote_rejects_unsupported_params(tmp_path: Path, fake_service: FakeService) -> None:
    image = tmp_path / "input.png"
    Image.new("RGB", (64, 64), (5, 6, 7)).save(image)
    with pytest.raises(RemoteError, match="mc-resolution"):
        cli_remote.generate("http://127.0.0.1:9999", str(image), mc_resolution=128)


def test_six_degradation_paths_are_documented() -> None:
    assert len(list(cli_remote.iter_error_cases())) == 6


@pytest.mark.parametrize("error", list(RemoteError(m) for m in cli_remote.iter_error_cases()))
def test_cli_falls_back_to_direct_on_any_remote_error(
    monkeypatch, capsys, tmp_path: Path, error: RemoteError
) -> None:
    image = tmp_path / "input.png"
    Image.new("RGB", (64, 64), (5, 6, 7)).save(image)
    monkeypatch.setattr(cli, "discover_server", lambda _: "http://127.0.0.1:9999")
    monkeypatch.setattr(cli, "remote_generate", lambda *a, **kw: (_ for _ in ()).throw(error))
    monkeypatch.setattr(cli, "generate_image", lambda *a, **kw: {"job_id": "direct-fallback", "mode": "direct"})

    code = cli.main(["generate", "-i", str(image), "--json"])
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["data"]["mode"] == "direct"
    assert "回落直调" in captured.err


def test_no_remote_flag_skips_discovery(monkeypatch, capsys, tmp_path: Path) -> None:
    image = tmp_path / "input.png"
    Image.new("RGB", (64, 64), (5, 6, 7)).save(image)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("--no-remote 不应触发服务发现")

    monkeypatch.setattr(cli, "discover_server", forbidden)
    monkeypatch.setattr(cli, "generate_image", lambda *a, **kw: {"job_id": "direct-only", "mode": "direct"})
    code = cli.main(["generate", "-i", str(image), "--no-remote", "--json"])
    assert code == 0
    assert json.loads(capsys.readouterr().out)["data"]["mode"] == "direct"
