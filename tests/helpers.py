"""Synthetic image builders shared by tests (no real user data)."""

from __future__ import annotations

import io

from PIL import Image, ImageDraw


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
