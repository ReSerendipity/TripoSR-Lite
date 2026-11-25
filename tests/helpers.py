"""Synthetic image builders shared by tests (no real user data)."""

from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image, ImageDraw


def fake_supervisor_path() -> Path:
    return Path(__file__).with_name("fake_supervisor.py")


def build_ready_adapter(tmp_path: Path, **overrides):
    """Adapter whose static prerequisites exist; the runtime probe is stubbed per test."""
    from triposr_lite.adapter import TripoSRAdapter

    model_dir = tmp_path / "models" / "triposr"
    model_dir.mkdir(parents=True)
    (model_dir / "config.yaml").write_text("cond_image_size: 512\n", encoding="utf-8")
    (model_dir / "model.ckpt").write_bytes(b"\0" * 2048)

    upstream = tmp_path / "external" / "TripoSR"
    upstream.mkdir(parents=True)
    (upstream / "run.py").write_text("# stub upstream entry\n", encoding="utf-8")

    params = dict(
        weights_dir=model_dir,
        min_ckpt_bytes=1024,
        runtime_python=sys.executable,
        supervisor=fake_supervisor_path(),
        upstream_dir=upstream,
        results_dir=tmp_path / "results",
        min_free_mib=512,
    )
    params.update(overrides)
    return TripoSRAdapter(**params)


def ok_runtime_probe(*_args, **_kwargs) -> dict:
    return {
        "ok": True,
        "code": "OK",
        "python": sys.executable,
        "torch": "2.11.0+cu128",
        "cuda_available": True,
        "device_name": "RTX test",
        "runtime_python_version": "3.12.10",
    }


def png_bytes(width: int = 256, height: int = 256, box: bool = True, rgb: tuple = (30, 90, 200)) -> bytes:
    image = Image.new("RGB", (width, height), (245, 245, 245))
    if box:
        draw = ImageDraw.Draw(image)
        left, top = width // 4, height // 4
        draw.rectangle([left, top, width - left, height - top], fill=rgb)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def jpeg_bytes(width: int = 320, height: int = 240) -> bytes:
    image = Image.new("RGB", (width, height), (210, 210, 210))
    ImageDraw.Draw(image).ellipse([60, 40, 260, 200], fill=(120, 40, 40))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def gif_bytes() -> bytes:
    image = Image.new("P", (128, 128), 0)
    buffer = io.BytesIO()
    image.save(buffer, format="GIF")
    return buffer.getvalue()


def alpha_png_bytes(size: int = 256) -> bytes:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse([40, 40, size - 40, size - 40], fill=(240, 120, 60, 255))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
