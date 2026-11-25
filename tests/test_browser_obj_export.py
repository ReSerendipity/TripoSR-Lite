"""Browser OBJ branch helpers: header contract and format selection without Playwright."""

import importlib.util
from pathlib import Path

BROWSER = Path(__file__).resolve().parents[1] / "scripts" / "browser_real.py"


def _browser():
    spec = importlib.util.spec_from_file_location("browser_real", BROWSER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _obj_text(
    *,
    header="# TripoSR-Lite OBJ export",
    source="model",
    label="TripoSR",
    note="上游 TripoSR 真实推理输出（本机执行）。",
    node="TRIPOSR_MESH",
    vertices=3,
    faces=2,
    declared=(3, 2),
) -> str:
    declared_text = f"# vertices: {declared[0]} faces: {declared[1]}" if declared else "# no counts"
    lines = [
        header,
        "# generated_at: 2026-11-25T20:00:00+08:00",
        f"# source: {source}",
        f"# label: {label}",
        f"# note: {note}",
        declared_text,
        "# units: arbitrary (TripoSR canonical space, Y-up)",
        f"o {node}",
    ]
    for index in range(vertices):
        lines.append(f"v {index:.1f} 0.0 0.0 1.0 0.0 0.0")
    for index in range(vertices):
        lines.append(f"vn 0.0 0.0 {index:.1f}")
    for index in range(faces):
        a = index % vertices + 1
        b = (index + 1) % vertices + 1
        c = (index + 2) % vertices + 1
        lines.append(f"f {a}//{a} {b}//{b} {c}//{c}")
    return "\n".join(lines) + "\n"


def _parse(tmp_path, text: str) -> dict:
    path = tmp_path / "sample.obj"
    path.write_text(text, encoding="utf-8")
    return _browser().obj_structure(path)


def test_obj_structure_accepts_model_export(tmp_path):
    parsed = _parse(tmp_path, _obj_text())

    assert parsed["header_ok"] is True
    assert parsed["source"] == "model"
    assert parsed["source_model"] is True
    assert parsed["label"] == "TripoSR"
    assert parsed["no_demo"] is True
    assert parsed["vertices"] == 3
    assert parsed["faces"] == 2
    assert parsed["declared_matches"] is True
    assert parsed["counts_nonempty"] is True
    assert parsed["object_name"] == "TRIPOSR_MESH"


def test_obj_structure_rejects_demo_export(tmp_path):
    parsed = _parse(
        tmp_path,
        _obj_text(
            source="demo",
            label="DEMO",
            note="程序生成的演示网格，不是 TripoSR 推理结果。",
            node="DEMO_NON_INFERENCE",
        ),
    )

    assert parsed["header_ok"] is True
    assert parsed["source_model"] is False
    assert parsed["no_demo"] is False
    assert parsed["counts_nonempty"] is True


def test_obj_structure_detects_header_and_count_mismatch(tmp_path):
    parsed = _parse(tmp_path, _obj_text(header="# wrong header", declared=(99, 98)))

    assert parsed["header_ok"] is False
    assert parsed["declared_matches"] is False
    assert parsed["vertices"] == 3
    assert parsed["faces"] == 2


def test_obj_structure_flags_demo_node_even_with_model_label(tmp_path):
    parsed = _parse(tmp_path, _obj_text(node="DEMO_NON_INFERENCE"))

    assert parsed["source_model"] is True
    assert parsed["no_demo"] is False


def test_resolve_formats_maps_both_and_single():
    browser = _browser()
    assert browser.resolve_formats("both") == ("obj", "glb")
    assert browser.resolve_formats("obj") == ("obj",)
    assert browser.resolve_formats("glb") == ("glb",)


def test_required_checks_follow_selected_formats():
    browser = _browser()
    obj_required = browser.required_checks(False, ("obj",))
    glb_required = browser.required_checks(False, ("glb",))
    both_required = browser.required_checks(False, ("obj", "glb"))
    prepare_required = browser.required_checks(True, ("obj", "glb"))

    assert "obj_artifact_valid" in obj_required
    assert "artifact_valid" not in obj_required
    assert "artifact_valid" in glb_required
    assert "obj_artifact_valid" not in glb_required
    assert set(obj_required) | set(glb_required) <= set(both_required)
    assert "export_obj_disabled" in prepare_required
    assert "obj_download_nonempty" not in prepare_required
