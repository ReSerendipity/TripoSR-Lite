"""Supervisor input staging: the app stages input.png inside the output dir it passes."""

import importlib.util
from pathlib import Path

SUPERVISOR_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_triposr_real.py"


def _supervisor():
    spec = importlib.util.spec_from_file_location("run_triposr_real", SUPERVISOR_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_input_reuses_image_already_in_output_dir(tmp_path):
    image = tmp_path / "input.png"
    image.write_bytes(b"png-bytes")

    staged = _supervisor().stage_input(image, tmp_path)

    assert staged == image
    assert staged.read_bytes() == b"png-bytes"
    assert [p.name for p in tmp_path.iterdir()] == ["input.png"]


def test_stage_input_copies_external_sample(tmp_path):
    sample = tmp_path / "sample" / "chair.png"
    sample.parent.mkdir()
    sample.write_bytes(b"png-bytes")
    out = tmp_path / "run"
    out.mkdir()

    staged = _supervisor().stage_input(sample, out)

    assert staged == out / "input.png"
    assert staged.read_bytes() == b"png-bytes"
    assert sample.is_file()
