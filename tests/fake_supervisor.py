"""Pytest-only stand-in for scripts/run_triposr_real.py.

It mirrors the supervisor contract the adapter depends on (argv flags, run.log
stage lines, metrics.json, mesh.glb) without touching torch, CUDA or weights.
Behaviour is selected with FAKE_SUPERVISOR_MODE.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

STAGE_LINES = [
    "2026-11-25 12:00:00,000 - INFO - Initializing model ...",
    "2026-11-25 12:00:01,000 - INFO - Initializing model finished in 1000.00ms.",
    "2026-11-25 12:00:01,001 - INFO - Processing images ...",
    "2026-11-25 12:00:02,000 - INFO - Processing images finished in 999.00ms.",
    "2026-11-25 12:00:02,001 - INFO - Running model ...",
    "2026-11-25 12:00:03,000 - INFO - Running model finished in 999.00ms.",
    "2026-11-25 12:00:03,001 - INFO - Extracting mesh ...",
    "2026-11-25 12:00:04,000 - INFO - Extracting mesh finished in 999.00ms.",
]


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--mc-resolution", type=int, default=256)
    parser.add_argument("--chunk-size", type=int, default=8192)
    parser.add_argument("--min-free-mib", type=int, default=7168)
    parser.add_argument("--model-dir", default="")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--no-remove-bg", action="store_true")
    parser.add_argument("--foreground-ratio", type=float, default=0.85)
    args = parser.parse_args()

    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write(
        out / "argv.json",
        {
            "argv": sys.argv,
            "cwd": os.getcwd(),
            "pythonpath": os.environ.get("PYTHONPATH"),
            "unbuffered": os.environ.get("PYTHONUNBUFFERED"),
            "remove_background": not args.no_remove_bg,
            "foreground_ratio": args.foreground_ratio,
            "min_free_mib": args.min_free_mib,
            "model_dir": args.model_dir,
            "image": args.image,
            "image_bytes": Path(args.image).stat().st_size,
        },
    )

    mode = os.environ.get("FAKE_SUPERVISOR_MODE", "ok")
    if mode in {"ok", "empty_mesh", "sleep"}:
        (out / "run.log").write_text("\n".join(STAGE_LINES) + "\n", encoding="utf-8")

    metrics = {
        "status": "MODEL_TESTED",
        "params": {
            "device": "cuda:0",
            "device_effective": "cpu",
            "mc_resolution": args.mc_resolution,
            "chunk_size": args.chunk_size,
            "remove_background": not args.no_remove_bg,
            "foreground_ratio": args.foreground_ratio,
            "model_dir": args.model_dir,
        },
        "timings": {
            "initializing_model": 1.0,
            "processing_images": 1.0,
            "running_model": 1.0,
            "extracting_mesh": 1.0,
        },
        "wall_seconds": 4.0,
        "upstream_wall_seconds": 3.5,
        "gpu": {"name": "fake-gpu", "peak_used_mib": 4200, "free_before_mib": 11000},
    }

    if mode == "ok":
        _write(out / "metrics.json", metrics)
        import trimesh

        mesh = trimesh.creation.icosphere(subdivisions=2, radius=0.5)
        mesh.export(out / "mesh.glb")
        return 0
    if mode == "empty_mesh":
        _write(out / "metrics.json", metrics)
        return 0
    if mode == "fail":
        metrics["status"] = "BLOCKED"
        metrics["reason"] = "upstream crashed"
        metrics["log_tail"] = "Traceback: boom"
        _write(out / "metrics.json", metrics)
        return 1
    if mode == "crash":
        return 1
    if mode == "gpu_busy":
        _write(out / "metrics.json", {"status": "BLOCKED", "reason": "GPU busy",
                                      "gpu_before": {"compute_apps": ["1234 python.exe"]}})
        return 3
    if mode == "low_vram":
        _write(out / "metrics.json", {"status": "BLOCKED", "reason": "insufficient VRAM",
                                      "gpu_before": {"free_mib": 900}})
        return 3
    if mode == "sleep":
        time.sleep(60)
        (out / "finished.marker").write_text("finished", encoding="utf-8")
        return 0
    raise SystemExit(f"unknown FAKE_SUPERVISOR_MODE={mode}")


if __name__ == "__main__":
    raise SystemExit(main())
