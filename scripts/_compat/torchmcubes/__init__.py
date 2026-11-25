"""CPU marching-cubes stand-in for the `torchmcubes` extension (compat shim).

Why this file exists
--------------------
TripoSR's upstream `tsr/models/isosurface.py` imports `torchmcubes`, which is a CUDA
extension built at install time. This machine has Visual Studio 18 Build Tools but **no
CUDA Toolkit / nvcc**, so the CMake build aborts inside torch's own `Caffe2Config.cmake`
("Your installed Caffe2 version uses CUDA but I cannot find the CUDA libraries"). The
work order explicitly allows keeping the real TripoSR forward pass and extracting the mesh
on the CPU instead.

This package is injected through `PYTHONPATH` so the **official** `run.py` executes
unmodified: only `torchmcubes.marching_cubes` is replaced, and it is replaced by
scikit-image's CPU marching cubes. The image encoder (DINOv2), transformer backbone,
triplane NeRF decoder, density and colour queries all still run as the real model on CUDA.

Only `marching_cubes` is imported by the upstream isosurface helper.
"""

from __future__ import annotations

import warnings

import numpy as np
import torch
from skimage import measure

__version__ = "0.1.0+cpu-shim"
HAS_CUDA = False


def _warn_cpu_fallback() -> None:
    warnings.warn(
        "torchmcubes CPU shim in use: marching cubes runs on the CPU (scikit-image); "
        "volumes are copied from CUDA to host memory.",
        RuntimeWarning,
        stacklevel=3,
    )


def marching_cubes(vol: torch.Tensor, thresh: float) -> tuple[torch.Tensor, torch.Tensor]:
    """Drop-in replacement for `torchmcubes.marching_cubes`.

    Convention: the returned vertices follow torchmcubes' axis order, i.e. the volume is
    indexed ``vol[k, j, i]`` from the extension's point of view and the vertices come back
    as ``(k, j, i)``. Upstream TripoSR then applies ``v_pos[..., [2, 1, 0]]`` to restore
    ``(i, j, k)`` before scaling into the renderer radius.
    """
    device = None
    if vol.is_cuda:
        _warn_cpu_fallback()
        device = vol.device
        work = vol.detach().to("cpu", torch.float32)
    else:
        work = vol.detach().to(torch.float32)

    volume = np.ascontiguousarray(work.numpy())
    verts, faces, _normals, _values = measure.marching_cubes(
        volume,
        level=float(thresh),
        allow_degenerate=False,
        method="lewiner",
    )
    # (i, j, k) -> (k, j, i) to match the extension's convention.
    verts = np.ascontiguousarray(verts[:, [2, 1, 0]])
    v_pos = torch.from_numpy(verts).to(torch.float32)
    t_pos_idx = torch.from_numpy(faces.astype(np.int64))
    if device is not None:
        v_pos = v_pos.to(device)
        t_pos_idx = t_pos_idx.to(device)
    return v_pos, t_pos_idx
