"""Input validation and the coarse foreground hint."""

import pytest

from meshmint import imaging, settings
from helpers import alpha_png_bytes, gif_bytes, jpeg_bytes, png_bytes


def test_png_validation_reports_real_dimensions():
    data = png_bytes(320, 200)
    info = imaging.validate_image_bytes(data, filename="a.png", declared_mime="image/png")
    assert info.format == "PNG"
    assert info.width == 320 and info.height == 200
    assert info.size_bytes == len(data)
    assert info.has_alpha is False
    assert info.warnings == []


def test_jpeg_accepted_and_mime_mismatch_is_a_warning():
    data = jpeg_bytes()
    info = imaging.validate_image_bytes(data, filename="b.jpg", declared_mime="image/png")
    assert info.format == "JPEG"
    assert info.width == 320 and info.height == 240
    assert any("不一致" in warning for warning in info.warnings)


def test_gif_is_rejected_with_machine_code():
    with pytest.raises(imaging.ImageValidationError) as exc:
        imaging.validate_image_bytes(gif_bytes(), filename="c.gif")
    assert exc.value.code == "UNSUPPORTED_FORMAT"
    assert "PNG" in exc.value.message


def test_corrupt_bytes_are_unreadable():
    with pytest.raises(imaging.ImageValidationError) as exc:
        imaging.validate_image_bytes(b"definitely not an image", filename="d.png")
    assert exc.value.code == "IMAGE_UNREADABLE"


def test_size_limit_is_enforced(monkeypatch):
    data = png_bytes(200, 200)
    monkeypatch.setenv("MESHMINT_MAX_IMAGE_BYTES", str(len(data) - 1))
    with pytest.raises(imaging.ImageValidationError) as exc:
        imaging.validate_image_bytes(data, filename="e.png")
    assert exc.value.code == "IMAGE_TOO_LARGE"
    assert exc.value.detail["size_bytes"] == len(data)


def test_tiny_image_rejected():
    with pytest.raises(imaging.ImageValidationError) as exc:
        imaging.validate_image_bytes(png_bytes(32, 32, box=False))
    assert exc.value.code == "IMAGE_TOO_SMALL"
    assert exc.value.detail["min_edge"] == settings.MIN_IMAGE_EDGE


def test_foreground_hint_from_background_colour():
    hint = imaging.estimate_foreground(png_bytes(256, 256, box=True))
    assert hint["method"] == "corner-color-distance"
    assert 0.15 < hint["foreground_ratio"] < 0.32
    assert hint["mask_data_url"].startswith("data:image/png;base64,")
    assert "不是分割结果" in hint["note"]
    assert "背景基准色 rgb(" in hint["background_hint"]
    assert hint["advice"]


def test_foreground_hint_prefers_alpha_when_present():
    hint = imaging.estimate_foreground(alpha_png_bytes(256))
    assert hint["method"] == "alpha-channel"
    assert 0.35 < hint["foreground_ratio"] < 0.75
    assert hint["threshold"] == 8
    assert "透明区域" in hint["background_hint"]
