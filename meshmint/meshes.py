"""Mesh records, the demo placeholder mesh, and OBJ / GLB export.

A demo mesh is always labelled as a placeholder. It is never presented as a
model result: the API carries `source: "demo"` and the export files embed the
same warning in their headers/names.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone

import numpy as np
import trimesh

from . import settings

DEMO_LABEL = "DEMO"
DEMO_NOTE = "程序生成的演示网格，不是 TripoSR 推理结果，也不代表任何真实输入。"
DEMO_NODE_NAME = "DEMO_NON_INFERENCE"
MODEL_NODE_NAME = "TRIPOSR_MESH"


class MeshExportError(Exception):
    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class MeshRecord:
    id: str
    mesh: trimesh.Trimesh
    source: str  # "demo" | "model"
    label: str
    note: str
    created_at: str = field(default_factory=utc_now)
    job_id: str | None = None

    @property
    def is_demo(self) -> bool:
        return self.source == "demo"

    @property
    def node_name(self) -> str:
        return DEMO_NODE_NAME if self.is_demo else MODEL_NODE_NAME

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "label": self.label,
            "note": self.note,
            "demo": self.is_demo,
            "created_at": self.created_at,
            "job_id": self.job_id,
            "stats": mesh_stats(self.mesh),
        }


def build_demo_mesh() -> trimesh.Trimesh:
    """Deterministic placeholder: a lumpy icosphere with baked vertex colors."""
    base = trimesh.creation.icosphere(subdivisions=3, radius=1.0)
    directions = np.asarray(base.vertices, dtype=np.float64)
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)

    x, y, z = directions[:, 0], directions[:, 1], directions[:, 2]
    radius = (
        0.46
        + 0.075 * np.sin(3.0 * x + 0.4) * np.cos(2.0 * y)
        + 0.045 * np.cos(4.0 * z + 0.9)
    )
    vertices = directions * radius[:, None]
    vertices[:, 1] += 0.04 * np.sin(2.0 * x)  # slight asymmetry so it reads as a placeholder

    z_min, z_max = vertices[:, 2].min(), vertices[:, 2].max()
    t = (vertices[:, 2] - z_min) / max(z_max - z_min, 1e-9)
    low = np.array([0.13, 0.62, 0.66])
    high = np.array([0.98, 0.62, 0.24])
    rgb = (low[None, :] * (1.0 - t[:, None]) + high[None, :] * t[:, None]) * 255.0
    colors = np.concatenate([rgb, np.full((len(vertices), 1), 255.0)], axis=1).astype(np.uint8)

    return trimesh.Trimesh(
        vertices=vertices,
        faces=np.asarray(base.faces, dtype=np.int64),
        vertex_colors=colors,
        process=False,
        metadata={"label": DEMO_LABEL, "note": DEMO_NOTE},
    )


def mesh_stats(mesh: trimesh.Trimesh) -> dict:
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    extents = np.asarray(mesh.extents, dtype=np.float64)
    stats = {
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "bounds": [[round(float(v), 6) for v in row] for row in bounds],
        "extents": [round(float(v), 6) for v in extents],
        "watertight": bool(mesh.is_watertight),
        "colors": str(getattr(mesh.visual, "kind", "none")),
    }
    if stats["watertight"]:
        stats["volume"] = round(float(mesh.volume), 6)
    return stats


def vertex_colors(mesh: trimesh.Trimesh) -> np.ndarray | None:
    try:
        kind = getattr(mesh.visual, "kind", None)
        if kind == "vertex":
            colors = np.asarray(mesh.visual.vertex_colors)
            if colors.ndim == 2 and colors.shape[0] == len(mesh.vertices) and colors.shape[1] >= 3:
                rgb = colors[:, :3].astype(np.float64)
                if rgb.max() > 1.0:
                    rgb = rgb / 255.0
                return rgb
    except Exception:  # noqa: BLE001 - colour metadata is optional
        return None
    return None


def mesh_payload(record: MeshRecord) -> dict:
    mesh = record.mesh
    positions = np.asarray(mesh.vertices, dtype=np.float32).reshape(-1).round(6)
    indices = np.asarray(mesh.faces, dtype=np.int64).reshape(-1).astype(np.uint32)
    colors = vertex_colors(mesh)
    payload = record.as_dict()
    payload.update(
        {
            "positions": positions.tolist(),
            "indices": indices.tolist(),
            "colors": colors.reshape(-1).round(4).tolist() if colors is not None else [],
        }
    )
    return payload


def _safe_name(record: MeshRecord) -> str:
    stem = "meshmint-demo" if record.is_demo else f"meshmint-{record.id}"
    return stem.replace(" ", "_")


def export_obj(record: MeshRecord, generated_at: str | None = None) -> bytes:
    mesh = record.mesh
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        raise MeshExportError("EMPTY_MESH", "网格为空，无法导出 OBJ。")

    colors = vertex_colors(mesh)
    normals = np.asarray(mesh.vertex_normals, dtype=np.float64)
    timestamp = generated_at or utc_now()

    header = [
        "# MeshMint-Lite OBJ export",
        f"# generated_at: {timestamp}",
        f"# source: {record.source}",
        f"# label: {record.label}",
        f"# note: {record.note}",
        f"# vertices: {len(mesh.vertices)} faces: {len(mesh.faces)}",
        "# units: arbitrary (TripoSR canonical space, Y-up)",
        f"o {record.node_name}",
    ]
    lines = header

    for index, vertex in enumerate(np.asarray(mesh.vertices, dtype=np.float64)):
        if colors is not None:
            rgb = colors[index]
            lines.append(
                "v {:.6f} {:.6f} {:.6f} {:.4f} {:.4f} {:.4f}".format(
                    float(vertex[0]), float(vertex[1]), float(vertex[2]),
                    float(rgb[0]), float(rgb[1]), float(rgb[2]),
                )
            )
        else:
            lines.append(
                "v {:.6f} {:.6f} {:.6f}".format(float(vertex[0]), float(vertex[1]), float(vertex[2]))
            )

    for normal in normals:
        lines.append("vn {:.6f} {:.6f} {:.6f}".format(float(normal[0]), float(normal[1]), float(normal[2])))

    for face in np.asarray(mesh.faces, dtype=np.int64):
        a, b, c = int(face[0]) + 1, int(face[1]) + 1, int(face[2]) + 1
        lines.append(f"f {a}//{a} {b}//{b} {c}//{c}")

    return ("\n".join(lines) + "\n").encode("utf-8")


def export_glb(record: MeshRecord) -> bytes:
    mesh = record.mesh
    if len(mesh.vertices) == 0 or len(mesh.faces) == 0:
        raise MeshExportError("EMPTY_MESH", "网格为空，无法导出 GLB。")

    colors = vertex_colors(mesh)
    geometry = trimesh.Trimesh(
        vertices=np.asarray(mesh.vertices, dtype=np.float64),
        faces=np.asarray(mesh.faces, dtype=np.int64),
        vertex_colors=(colors * 255.0).astype(np.uint8) if colors is not None else None,
        process=False,
    )
    scene = trimesh.Scene()
    scene.add_geometry(geometry, node_name=record.node_name, geom_name=record.node_name)

    try:
        data = scene.export(file_type="glb")
    except Exception as exc:  # noqa: BLE001 - surface the exporter failure honestly
        raise MeshExportError("GLB_EXPORT_FAILED", f"GLB 导出失败：{exc}") from exc
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not data or data[:4] != b"glTF":
        raise MeshExportError("GLB_EXPORT_FAILED", "GLB 导出结果缺少 glTF 文件头。", {"bytes": len(data or b"")})
    return bytes(data)


def inspect_glb(data: bytes) -> dict:
    """Parse the GLB container; used by tests and the export self-check."""
    if len(data) < 20 or data[:4] != b"glTF":
        raise MeshExportError("GLB_INVALID", "缺少 glTF magic。")
    version, declared = struct.unpack("<II", data[4:12])
    if version != 2:
        raise MeshExportError("GLB_INVALID", f"GLB 版本应为 2，实际 {version}。")
    if declared != len(data):
        raise MeshExportError(
            "GLB_INVALID",
            f"GLB 声明长度 {declared} 与实测 {len(data)} 不一致。",
        )

    json_length, json_type = struct.unpack("<II", data[12:20])
    if json_type != 0x4E4F534A:
        raise MeshExportError("GLB_INVALID", "首个 chunk 不是 JSON。")
    document = json.loads(data[20 : 20 + json_length].decode("utf-8"))

    offset = 20 + json_length
    bin_chunk = None
    if offset + 8 <= len(data):
        bin_length, bin_type = struct.unpack("<II", data[offset : offset + 8])
        if bin_type == 0x004E4942:
            bin_chunk = data[offset + 8 : offset + 8 + bin_length]

    node_names = [node.get("name") for node in document.get("nodes", [])]
    mesh_names = [mesh.get("name") for mesh in document.get("meshes", [])]
    return {
        "version": version,
        "length": declared,
        "asset": document.get("asset", {}),
        "node_names": node_names,
        "mesh_names": mesh_names,
        "has_bin_chunk": bin_chunk is not None,
        "mesh_count": len(document.get("meshes", [])),
        "accessor_count": len(document.get("accessors", [])),
    }


def load_obj_vertices(data: bytes) -> int:
    """Count `v` lines in an OBJ payload (test helper, no external deps)."""
    text = data.decode("utf-8")
    return sum(1 for line in text.splitlines() if line.startswith("v "))
