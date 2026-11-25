"""HTTP layer: validation errors, NOT_READY flow, exports and static UI."""

import pytest

from helpers import alpha_png_bytes, gif_bytes, png_bytes


def _upload(client, data: bytes, name: str = "test.png", mime: str = "image/png"):
    return client.post("/api/jobs", files={"file": (name, data, mime)})


def test_health_reports_verification_state(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["app"] == "TripoSR-Lite"
    assert payload["verification_status"] == "CODE_READY_MODEL_UNTESTED"


def test_model_endpoint_exposes_reasons_and_licences(client):
    response = client.get("/api/model")
    assert response.status_code == 200
    payload = response.json()
    probe = payload["probe"]
    assert probe["state"] == "NOT_READY"
    codes = {reason["code"] for reason in probe["reasons"]}
    assert {"MISSING_WEIGHTS", "MISSING_CONFIG"} <= codes
    assert payload["model"]["weights_bundled"] is False
    assert payload["model"]["code_license"] == "MIT"
    assert payload["model"]["weights_license"] == "MIT"
    assert "stabilityai/TripoSR" in payload["model"]["download_command"]


def test_demo_endpoint_is_explicitly_labelled(client):
    response = client.get("/api/demo")
    assert response.status_code == 200
    payload = response.json()
    assert payload["demo"] is True
    assert payload["label"] == "DEMO"
    assert "不是 TripoSR 推理结果" in payload["note"]
    assert len(payload["mesh"]["positions"]) == 3 * 642
    assert payload["mesh"]["stats"]["faces"] == 1280


def test_inspect_returns_foreground_hint(client):
    response = client.post("/api/inspect", files={"file": ("test.png", png_bytes(), "image/png")})
    assert response.status_code == 200
    payload = response.json()
    assert payload["image"]["format"] == "PNG"
    assert payload["image"]["width"] == 256
    hint = payload["foreground"]
    assert hint["method"] in {"corner-color-distance", "alpha-channel"}
    assert 0.0 <= hint["foreground_ratio"] <= 1.0
    assert hint["mask_data_url"].startswith("data:image/png;base64,")


def test_inspect_rejects_non_image(client):
    response = client.post("/api/inspect", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IMAGE_UNREADABLE"


def test_inspect_rejects_gif(client):
    response = client.post("/api/inspect", files={"file": ("anim.gif", gif_bytes(), "image/gif")})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "UNSUPPORTED_FORMAT"


def test_not_ready_job_flow_and_obj_export(client):
    response = _upload(client, png_bytes())
    assert response.status_code == 200
    payload = response.json()
    job = payload["job"]
    assert job["status"] == "NOT_READY"
    assert job["demo"] is True
    assert job["error_code"] == "NOT_READY"
    assert payload["mesh"]["demo"] is True
    assert payload["probe"]["state"] == "NOT_READY"

    detail = client.get(f"/api/jobs/{job['id']}")
    assert detail.status_code == 200
    assert detail.json()["job"]["status"] == "NOT_READY"

    mesh = client.get(f"/api/jobs/{job['id']}/mesh")
    assert mesh.status_code == 200
    assert mesh.json()["demo"] is True

    obj = client.get(f"/api/jobs/{job['id']}/export?format=obj")
    assert obj.status_code == 200
    assert obj.headers["content-type"].startswith("text/plain")
    assert "triposr_lite-demo.obj" in obj.headers["content-disposition"]
    assert obj.headers["x-triposr-demo"] == "true"
    assert obj.content.startswith(b"# TripoSR-Lite OBJ export")
    assert b"# source: demo" in obj.content
    assert b"# label: DEMO" in obj.content
    assert obj.content.count(b"\nv ") == 642
    assert obj.content.count(b"\nf ") == 1280


def test_glb_export_header(client):
    response = _upload(client, alpha_png_bytes(), name="cutout.png")
    job = response.json()["job"]
    glb = client.get(f"/api/jobs/{job['id']}/export?format=glb")
    assert glb.status_code == 200
    assert glb.headers["content-type"] == "model/gltf-binary"
    assert glb.content[:4] == b"glTF"
    assert glb.headers["x-triposr-source"] == "demo"


def test_invalid_export_format_is_rejected(client):
    response = _upload(client, png_bytes())
    job = response.json()["job"]
    bad = client.get(f"/api/jobs/{job['id']}/export?format=stl")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "INVALID_FORMAT"


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404
    assert client.get("/api/jobs/nope/export?format=obj").status_code == 404
    assert client.post("/api/jobs/nope/cancel").status_code == 404


def test_upload_limit_is_enforced_by_api(client, monkeypatch):
    monkeypatch.setenv("TRIPOSR_MAX_IMAGE_BYTES", "4096")
    response = _upload(client, b"\x89PNG\r\n\x1a\n" + b"0" * 5000)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IMAGE_TOO_LARGE"


def test_static_ui_and_threejs_are_served(client):
    index = client.get("/")
    assert index.status_code == 200
    assert "TripoSR" in index.text
    assert 'type="importmap"' in index.text
    assert "DEMO · 非推理结果" in index.text

    app_js = client.get("/assets/app.js")
    assert app_js.status_code == 200
    assert "OrbitControls" in app_js.text
    assert "DEMO" in app_js.text

    three = client.get("/assets/vendor/three.module.js")
    assert three.status_code == 200
    assert len(three.content) > 100_000
    assert b"THREE" in three.content

    controls = client.get("/assets/vendor/OrbitControls.js")
    assert controls.status_code == 200
    assert b"OrbitControls" in controls.content


def test_export_urls_in_job_payload_match_routes(client):
    response = _upload(client, png_bytes())
    job = response.json()["job"]
    assert job["mesh_url"] == f"/api/jobs/{job['id']}/mesh"
    assert job["export"]["obj"] == f"/api/jobs/{job['id']}/export?format=obj"
    assert job["export"]["glb"] == f"/api/jobs/{job['id']}/export?format=glb"
