"""Run the official TripoSR `run.py` end-to-end and record reproducible metrics.

This script is a supervisor, not a re-implementation: it executes
`external/TripoSR/run.py` as a subprocess, with `scripts/_compat` prepended to
`PYTHONPATH` so the CPU marching-cubes shim replaces `torchmcubes`. Everything else
(model load, rembg cutout, DINOv2 encode, triplane decode, mesh export) is upstream code.

It records: GPU state before loading, peak GPU memory (nvidia-smi sampling), upstream
stage timings parsed from the official log, exit code, output files and mesh checks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
UPSTREAM = PROJECT / "external" / "TripoSR"
RUN_PY = UPSTREAM / "run.py"
COMPAT = PROJECT / "scripts" / "_compat"

STAGE_RE = re.compile(r"INFO - (.+) finished in ([0-9.]+)ms\.")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def nvidia_smi(args: list[str]) -> str:
    out = subprocess.run(
        ["nvidia-smi", *args], capture_output=True, text=True, timeout=30, check=True
    )
    return out.stdout.strip()


def gpu_snapshot() -> dict:
    total, used = [
        int(x) for x in nvidia_smi(["--query-gpu=memory.total,memory.used", "--format=csv,noheader,nounits"]).split(",")
    ]
    name = nvidia_smi(["--query-gpu=name", "--format=csv,noheader,nounits"])
    driver = nvidia_smi(["--query-gpu=driver_version", "--format=csv,noheader,nounits"])
    apps = nvidia_smi(["--query-compute-apps=pid,process_name,used_memory", "--format=csv,noheader,nounits"])
    return {
        "name": name,
        "driver": driver,
        "total_mib": total,
        "used_mib": used,
        "free_mib": total - used,
        "compute_apps": [line for line in apps.splitlines() if line.strip()],
    }


class VramSampler(threading.Thread):
    def __init__(self, interval: float = 0.5) -> None:
        super().__init__(daemon=True)
        self.interval = interval
        self._halt = threading.Event()
        self.samples: list[int] = []
        self.error: str | None = None

    def run(self) -> None:
        while not self._halt.is_set():
            try:
                used = int(
                    nvidia_smi(["--query-gpu=memory.used", "--format=csv,noheader,nounits"])
                )
                self.samples.append(used)
            except Exception as exc:  # noqa: BLE001 - sampling must never kill the run
                self.error = f"{type(exc).__name__}: {exc}"
            time.sleep(self.interval)

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=10)


def parse_stage_times(log_text: str) -> dict:
    timings = {}
    for match in STAGE_RE.finditer(log_text):
        label = match.group(1).strip().lower().replace(" ", "_")
        timings[f"{label}_ms"] = float(match.group(2))
    return timings


def write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--mc-resolution", type=int, default=256)
    parser.add_argument("--chunk-size", type=int, default=8192)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--min-free-mib", type=int, default=7168, help="hard gate before model load")
    parser.add_argument("--hf-endpoint", default=os.environ.get("HF_ENDPOINT", "https://hf-mirror.com"))
    args = parser.parse_args()

    out = Path(args.output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    image = Path(args.image).resolve()
    if not RUN_PY.is_file():
        print(f"FATAL: upstream run.py missing at {RUN_PY}", file=sys.stderr)
        return 2
    if not image.is_file():
        print(f"FATAL: input image missing at {image}", file=sys.stderr)
        return 2

    started = time.time()
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    before = gpu_snapshot()
    write_json(out / "gpu-before.json", before)
    if before["compute_apps"]:
        print(f"BLOCKED: other GPU compute processes active: {before['compute_apps']}", file=sys.stderr)
        write_json(out / "metrics.json", {"status": "BLOCKED", "reason": "GPU busy", "gpu_before": before})
        return 3
    if before["free_mib"] < args.min_free_mib:
        print(
            f"BLOCKED: free VRAM {before['free_mib']} MiB < gate {args.min_free_mib} MiB",
            file=sys.stderr,
        )
        write_json(out / "metrics.json", {"status": "BLOCKED", "reason": "insufficient VRAM", "gpu_before": before})
        return 3

    # Input sample is fixed next to the outputs so the run is self-contained.
    input_copy = out / "input.png"
    shutil.copyfile(image, input_copy)
    input_info = {
        "source": str(image),
        "path": str(input_copy),
        "bytes": input_copy.stat().st_size,
        "sha256": sha256(input_copy),
    }

    upstream_out = out / "upstream"
    cmd = [
        sys.executable,
        str(RUN_PY),
        str(input_copy),
        "--pretrained-model-name-or-path",
        str(PROJECT / "models" / "triposr"),
        "--output-dir",
        str(upstream_out),
        "--mc-resolution",
        str(args.mc_resolution),
        "--chunk-size",
        str(args.chunk_size),
        "--device",
        args.device,
        "--model-save-format",
        "glb",
    ]

    env = os.environ.copy()
    env["PYTHONPATH"] = str(COMPAT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONUNBUFFERED"] = "1"
    env["HF_ENDPOINT"] = args.hf_endpoint
    env["HF_HUB_DISABLE_XET"] = "1"

    sampler = VramSampler()
    sampler.start()
    run_started = time.perf_counter()
    log_path = out / "run.log"
    with log_path.open("w", encoding="utf-8") as fh:
        proc = subprocess.run(cmd, cwd=str(PROJECT), env=env, stdout=fh, stderr=subprocess.STDOUT, text=True)
    run_seconds = time.perf_counter() - run_started
    sampler.stop()
    after = gpu_snapshot()
    write_json(out / "gpu-after.json", after)

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    stage_ms = parse_stage_times(log_text)
    peak = max(sampler.samples) if sampler.samples else None

    metrics: dict = {
        "status": "PARTIAL",
        "started_at": started_at,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "wall_seconds": round(time.time() - started, 3),
        "upstream_wall_seconds": round(run_seconds, 3),
        "command": cmd,
        "python": sys.version.split()[0],
        "input": input_info,
        "params": {
            "device": args.device,
            "mc_resolution": args.mc_resolution,
            "chunk_size": args.chunk_size,
            "save_format": "glb",
            "remove_background": True,
            "cond_image_size": 512,
            "precision": "float32",
            "hf_endpoint": args.hf_endpoint,
            "cuda_marching_cubes": False,
            "marching_cubes_backend": "scikit-image CPU shim via PYTHONPATH (scripts/_compat/torchmcubes)",
        },
        "gpu": {
            "name": before["name"],
            "driver": before["driver"],
            "total_mib": before["total_mib"],
            "free_before_mib": before["free_mib"],
            "peak_used_mib": peak,
            "peak_delta_mib": (peak - before["used_mib"]) if peak is not None else None,
            "samples": len(sampler.samples),
            "sampler_error": sampler.error,
        },
        "timings": {k: round(v / 1000.0, 3) for k, v in stage_ms.items()},
        "process": {"exit_code": proc.returncode, "log": str(log_path)},
    }

    if proc.returncode != 0:
        metrics["status"] = "BLOCKED"
        metrics["log_tail"] = "\n".join(log_text.splitlines()[-40:])
        write_json(out / "metrics.json", metrics)
        print(f"FAILED: upstream exit code {proc.returncode}; see {log_path}", file=sys.stderr)
        return proc.returncode or 1

    # ---- output validation -------------------------------------------------------
    glb_src = upstream_out / "0" / "mesh.glb"
    if not glb_src.is_file():
        metrics["status"] = "BLOCKED"
        metrics["reason"] = f"missing upstream output {glb_src}"
        write_json(out / "metrics.json", metrics)
        print(f"FAILED: {glb_src} not produced", file=sys.stderr)
        return 4

    glb = out / "mesh.glb"
    shutil.copyfile(glb_src, glb)
    obj = out / "mesh.obj"

    import numpy as np
    import trimesh

    mesh = trimesh.load(glb, force="mesh")
    checks = {
        "glb_bytes": glb.stat().st_size,
        "glb_magic": glb.open("rb").read(4).decode("ascii", "replace"),
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "vertex_colors": bool(getattr(mesh.visual, "vertex_colors", None) is not None),
        "bbox_min": [round(float(x), 4) for x in mesh.bounds[0]],
        "bbox_max": [round(float(x), 4) for x in mesh.bounds[1]],
        "volume": round(float(abs(mesh.volume)), 6),
    }
    mesh.export(obj)
    obj_mesh = trimesh.load(obj, force="mesh")
    checks["obj_bytes"] = obj.stat().st_size
    checks["obj_parsed_vertices"] = int(len(obj_mesh.vertices))
    checks["obj_parsed_faces"] = int(len(obj_mesh.faces))
    checks["obj_header"] = obj.read_text(encoding="utf-8", errors="replace")[:60].splitlines()[0]

    metrics["status"] = "MODEL_TESTED" if checks["vertices"] > 0 and checks["faces"] > 0 else "BLOCKED"
    metrics["outputs"] = {str(glb): checks["glb_bytes"], str(obj): checks["obj_bytes"]}
    metrics["mesh"] = checks
    metrics["versions"] = {
        "python": sys.version.split()[0],
        "torch": __import__("torch").__version__,
        "transformers": __import__("transformers").__version__,
        "trimesh": trimesh.__version__,
        "scikit-image": __import__("skimage").__version__,
        "numpy": np.__version__,
    }
    write_json(out / "metrics.json", metrics)

    print(json.dumps({"status": metrics["status"], "exit_code": proc.returncode, "mesh": checks}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

