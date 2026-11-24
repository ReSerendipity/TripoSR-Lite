"""Image validation and a coarse foreground hint.

The hint is deliberately *not* a segmentation model: TripoSR runs rembg before
inference, and this module only gives the user an early sanity check on whether
the subject occupies a reasonable part of the frame.
"""

from __future__ import annotations

import base64
import io
from dataclasses import asdict, dataclass, field

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from . import settings


class ImageValidationError(Exception):
    """Raised with a stable machine code so the UI can explain the failure."""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "detail": self.detail}


@dataclass
class ImageInfo:
    filename: str
    declared_mime: str
    width: int
    height: int
    format: str
    mode: str
    size_bytes: int
    has_alpha: bool
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _sniff(data: bytes) -> Image.Image:
    try:
        probe = Image.open(io.BytesIO(data))
        probe.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageValidationError(
            "IMAGE_UNREADABLE",
            "无法解析该文件为有效图片，可能已损坏或不是图片。",
            {"error": str(exc)},
        ) from exc
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise ImageValidationError(
            "IMAGE_UNREADABLE",
            "图片解码失败，可能已损坏。",
            {"error": str(exc)},
        ) from exc
    return image


def validate_image_bytes(
    data: bytes,
    filename: str | None = None,
    declared_mime: str | None = None,
) -> ImageInfo:
    if not data:
        raise ImageValidationError("IMAGE_EMPTY", "上传内容为空。")

    limit = settings.max_image_bytes()
    if len(data) > limit:
        raise ImageValidationError(
            "IMAGE_TOO_LARGE",
            f"图片体积 {len(data) / 1048576:.1f} MB 超过上限 {limit / 1048576:.0f} MB。",
            {"size_bytes": len(data), "limit_bytes": limit},
        )

    image = _sniff(data)
    fmt = (image.format or "").upper()
    if fmt not in settings.ALLOWED_FORMATS:
        raise ImageValidationError(
            "UNSUPPORTED_FORMAT",
            f"只支持 PNG 与 JPEG，当前格式为 {fmt or '未知'}。",
            {"format": fmt, "allowed": list(settings.ALLOWED_FORMATS)},
        )

    width, height = image.size
    if min(width, height) < settings.MIN_IMAGE_EDGE:
        raise ImageValidationError(
            "IMAGE_TOO_SMALL",
            f"图片边长过小（{width}×{height}），最短边至少 {settings.MIN_IMAGE_EDGE}px。",
            {"width": width, "height": height, "min_edge": settings.MIN_IMAGE_EDGE},
        )
    if max(width, height) > settings.MAX_IMAGE_EDGE:
        raise ImageValidationError(
            "IMAGE_TOO_LARGE_DIMENSIONS",
            f"图片分辨率过大（{width}×{height}），最长边上限 {settings.MAX_IMAGE_EDGE}px。",
            {"width": width, "height": height, "max_edge": settings.MAX_IMAGE_EDGE},
        )

    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    warnings: list[str] = []
    expected_mime = {"PNG": "image/png", "JPEG": "image/jpeg"}.get(fmt)
    if (
        declared_mime
        and declared_mime.startswith("image/")
        and expected_mime
        and declared_mime != expected_mime
    ):
        warnings.append(f"声明类型 {declared_mime} 与实际格式 {fmt} 不一致，按实际格式处理。")
    if fmt == "JPEG" and has_alpha:
        warnings.append("JPEG 不支持透明通道，已按不透明图片处理。")

    return ImageInfo(
        filename=filename or "upload",
        declared_mime=declared_mime or "application/octet-stream",
        width=width,
        height=height,
        format=fmt,
        mode=image.mode,
        size_bytes=len(data),
        has_alpha=has_alpha,
        warnings=warnings,
    )


def _png_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


MASK_RGB = (79, 209, 197)


def _mask_image(mask_arr: np.ndarray) -> Image.Image:
    """Tint the foreground mask so it reads as a hint overlay, not a photo."""
    height, width = mask_arr.shape
    rgba = np.zeros((height, width, 4), dtype=np.uint8)
    rgba[mask_arr] = (*MASK_RGB, 215)
    return Image.fromarray(rgba, mode="RGBA")


def _mask_from_alpha(image: Image.Image, limit: int = 256) -> dict:
    rgba = ImageOps.exif_transpose(image).convert("RGBA")
    rgba.thumbnail((limit, limit))
    alpha = np.asarray(rgba.getchannel("A"), dtype=np.uint8)
    mask_arr = alpha > 8
    ratio = float(mask_arr.mean())
    return {
        "method": "alpha-channel",
        "threshold": 8,
        "foreground_ratio": round(ratio, 4),
        "background_rgb": None,
        "background_hint": f"背景：alpha=0 的透明区域（{(1 - ratio) * 100:.1f}%）；前景为不透明像素。",
        "mask_data_url": _png_data_url(_mask_image(mask_arr)),
        "note": "按图片自带 alpha 通道统计前景占比；TripoSR 推理时会用 rembg 重新抠图。",
    }


def _mask_from_corners(image: Image.Image, limit: int = 256, threshold: int = 28) -> dict:
    rgb = ImageOps.exif_transpose(image).convert("RGB")
    rgb.thumbnail((limit, limit))
    arr = np.asarray(rgb, dtype=np.float32)
    h, w, _ = arr.shape
    patch = 8
    corners = np.concatenate(
        [
            arr[:patch, :patch].reshape(-1, 3),
            arr[:patch, -patch:].reshape(-1, 3),
            arr[-patch:, :patch].reshape(-1, 3),
            arr[-patch:, -patch:].reshape(-1, 3),
        ]
    )
    background = np.median(corners, axis=0)
    distance = np.sqrt(((arr - background) ** 2).sum(axis=2))
    mask_arr = distance > threshold
    ratio = float(mask_arr.mean())
    background_rgb = [int(v) for v in background.round().tolist()]
    return {
        "method": "corner-color-distance",
        "threshold": threshold,
        "foreground_ratio": round(ratio, 4),
        "background_rgb": background_rgb,
        "background_hint": (
            f"背景基准色 rgb({background_rgb[0]}, {background_rgb[1]}, {background_rgb[2]})"
            "（取四角 8×8 中值）；与之差异较大的像素记为前景。"
        ),
        "mask_data_url": _png_data_url(_mask_image(mask_arr)),
        "note": (
            "以四角颜色为背景基准的粗略前景估计，不是分割结果；"
            "纯色/渐变背景更可靠，复杂背景仅作提示。"
        ),
    }


def estimate_foreground(data: bytes) -> dict:
    """Return a coarse foreground ratio plus a small mask preview."""
    image = Image.open(io.BytesIO(data))
    image.load()
    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    hint = _mask_from_alpha(image) if has_alpha else _mask_from_corners(image)

    ratio = hint["foreground_ratio"]
    if ratio < 0.05:
        hint["advice"] = "前景占比很低，主体可能过小或被背景吞掉，建议裁剪后重试。"
    elif ratio > 0.95:
        hint["advice"] = "几乎整张图都被判为前景，背景可能不是纯色；生成质量可能受影响。"
    else:
        hint["advice"] = "前景占比处于常见范围；推理前仍会重新抠图。"
    return hint
