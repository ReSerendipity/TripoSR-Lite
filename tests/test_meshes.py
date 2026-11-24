"""DEMO mesh + OBJ/GLB export structure and round-trips."""

import io
import struct

import numpy as np
import pytest
import trimesh

from meshmint.meshes import (
    DEMO_NODE_NAME,
    MeshExportError,
    MeshRecord,
    build_demo_mesh,
    export_glb,
    export_obj,
    inspect_glb,
    mesh_payload,
    mesh_stats,
)


def _record(source: str = "demo", mesh=None) -> MeshRecord:
    return MeshRecord(
        id="demo" if source == "demo" else "job-test",
        mesh=mesh if mesh is not None else build_demo_mesh(),
        source=source,
        label="DEMO" if source == "demo" else "TripoSR",
        note="demo note",
    )


def test_demo_mesh_is_deterministic():
    first = build_demo_mesh()
    second = build_demo_mesh()
    assert len(first.vertices) == 642
    assert len(first.faces) == 1280
    assert np.allclose(first.vertices, second.vertices)
    assert first.vertex_normals.shape == first.vertices.shape
    assert first.visual.kind == "vertex"


def test_payload_flags_demo_and_matches_buffers():
    record = _record()
    payload = mesh_payload(record)
    stats = mesh_stats(record.mesh)

    assert payload["demo"] is True
    assert payload["label"] == "DEMO"
    assert payload["source"] == "demo"
    assert payload["stats"]["vertices"] == 642
    assert payload["stats"]["faces"] == 1280
    assert len(payload["positions"]) == 3 * 642
    assert len(payload["indices"]) == 3 * 1280
    assert len(payload["colors"]) == 3 * 642
    assert all(0.0 <= value <= 1.0 for value in payload["colors"])
    assert stats["watertight"] is True


def test_obj_export_has_headers_and_valid_indices():
    record = _record()
    data = export_obj(record, generated_at="2026-11-24T00:00:00+00:00")
    text = data.decode("utf-8")
    lines = text.splitlines()

    assert lines[0] == "# MeshMint-Lite OBJ export"
    assert "# source: demo" in text
    assert "# note: demo note" in text
    assert "# vertices: 642 faces: 1280" in text
    assert f"o {DEMO_NODE_NAME}" in text
    assert "NOT" in text or "demo" in text.lower()

    vertex_lines = [line for line in lines if line.startswith("v ")]
    normal_lines = [line for line in lines if line.startswith("vn ")]
    face_lines = [line for line in lines if line.startswith("f ")]
    assert len(vertex_lines) == 642
    assert len(normal_lines) == 642
    assert len(face_lines) == 1280

    for line in vertex_lines:
        parts = line.split()
        assert len(parts) == 7  # v + xyz + rgb
        assert all(0.0 <= float(value) <= 1.0 for value in parts[4:])

    for line in face_lines:
        parts = line.split()[1:]
        assert len(parts) == 3
        for token in parts:
            index = int(token.split("//")[0])
            assert 1 <= index <= 642
            assert token.split("//")[1] == token.split("//")[0]


def test_obj_round_trips_through_trimesh():
    data = export_obj(_record())
    loaded = trimesh.load(io.BytesIO(data), file_type="obj", process=False)
    assert isinstance(loaded, trimesh.Trimesh)
    assert len(loaded.vertices) == 642
    assert len(loaded.faces) == 1280


def test_glb_container_and_json_chunk_are_valid():
    data = export_glb(_record())
    info = inspect_glb(data)

    assert data[:4] == b"glTF"
    assert info["version"] == 2
    assert info["length"] == len(data)
    assert info["has_bin_chunk"] is True
    assert info["mesh_count"] == 1
    assert DEMO_NODE_NAME in info["node_names"]
    assert DEMO_NODE_NAME in info["mesh_names"]
    assert info["asset"]["version"] == "2.0"


def test_glb_round_trips_and_keeps_vertex_colours():
    data = export_glb(_record())
    scene = trimesh.load(io.BytesIO(data), file_type="glb")
    assert len(scene.geometry) == 1
    geometry = next(iter(scene.geometry.values()))
    assert len(geometry.vertices) == 642
    assert len(geometry.faces) == 1280
    assert geometry.visual.kind == "vertex"


def test_model_record_gets_non_demo_node_name():
    record = _record(source="model")
    data = export_glb(record)
    assert "TRIPOSR_MESH" in inspect_glb(data)["node_names"]
    assert inspect_glb(data)["node_names"] != [DEMO_NODE_NAME]


def test_empty_mesh_export_is_rejected():
    empty = trimesh.Trimesh()
    with pytest.raises(MeshExportError) as obj_exc:
        export_obj(MeshRecord(id="x", mesh=empty, source="model", label="x", note="x"))
    assert obj_exc.value.code == "EMPTY_MESH"

    with pytest.raises(MeshExportError) as glb_exc:
        export_glb(MeshRecord(id="x", mesh=empty, source="model", label="x", note="x"))
    assert glb_exc.value.code == "EMPTY_MESH"


def test_inspect_glb_rejects_bad_magic():
    with pytest.raises(MeshExportError) as exc:
        inspect_glb(b"not-a-glb-file-at-all")
    assert exc.value.code == "GLB_INVALID"

    broken = bytearray(export_glb(_record()))
    struct.pack_into("<I", broken, 8, len(broken) + 4)
    with pytest.raises(MeshExportError) as exc:
        inspect_glb(bytes(broken))
    assert "声明长度" in exc.value.message
