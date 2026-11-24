"""Model manifest: upstream URLs, licences, sizes and download commands.

This file is the single source of truth that the API, README and
docs/MODELS.md are checked against. Weight files are never bundled.
"""

from __future__ import annotations

import copy

from . import settings

MODEL_CARD: dict = {
    "id": "triposr",
    "display_name": "TripoSR",
    "role": "single-image to 3D mesh (base model)",
    "upstream_code_url": "https://github.com/VAST-AI-Research/TripoSR",
    "upstream_weights_url": "https://huggingface.co/stabilityai/TripoSR",
    "code_license": "MIT",
    "weights_license": "MIT",
    "license_note": (
        "TripoSR README states the MIT licence covers source code, pretrained models "
        "and the online demo. MeshMint-Lite ships shell code only and never redistributes weights."
    ),
    "training_data": {
        "name": "Objaverse (curated subset)",
        "license": "CC-BY",
        "note": "Model card states the curated training subset is available under CC-BY; attribution kept in NOTICE.",
    },
    "official_hardware_claim": {
        "vram_gb": 6,
        "text": "Official README: 'The default options takes about 6GB VRAM for a single image input.'",
        "basis": "official-statement",
        "measured_here": False,
    },
    "speed_claim": {
        "text": "Official README: less than 0.5 seconds on an NVIDIA A100 GPU (forward pass).",
        "basis": "official-statement",
        "measured_here": False,
    },
    "inputs": {
        "formats": ["PNG", "JPEG"],
        "cond_image_size": 512,
        "note": "Upstream resizes to 512×512 (config cond_image_size), removes background via rembg, foreground ratio 0.85.",
    },
    "outputs": {
        "formats": ["obj", "glb"],
        "mesh_type": "triplane NeRF marching-cubes mesh with vertex colours",
        "default_mc_resolution": 256,
        "default_chunk_size": 8192,
    },
    "weight_files": [
        {
            "filename": "model.ckpt",
            "expected_bytes": settings.EXPECTED_MODEL_CKPT_BYTES,
            "expected_label": "1.68 GB (1.56 GiB)",
            "required": True,
            "license": "MIT",
            "redistributed": False,
        },
        {
            "filename": "config.yaml",
            "expected_bytes": 987,
            "expected_label": "987 B",
            "required": True,
            "license": "MIT",
            "redistributed": False,
        },
    ],
    "auxiliary_weights": [
        {
            "id": "facebook/dino-vitb16",
            "role": "image tokenizer (DINOv2 ViT-B/16)",
            "url": "https://huggingface.co/facebook/dino-vitb16",
            "license": "Apache-2.0",
            "approx_bytes": 330_000_000,
            "auto_download": True,
            "redistributed": False,
            "note": "Transformers downloads it on first model load (config.yaml image_tokenizer).",
        }
    ],
    "runtime_assets": [
        {
            "id": "rembg / u2net",
            "role": "background removal before inference",
            "url": "https://github.com/danielgatis/rembg",
            "license": "rembg MIT; u2net weights from U-2-Net (Apache-2.0)",
            "approx_bytes": 176_000_000,
            "auto_download": True,
            "redistributed": False,
            "note": "Only downloaded when the upstream inference path runs; can be skipped with --no-remove-bg.",
        }
    ],
    "min_ckpt_bytes": settings.min_model_ckpt_bytes(),
    "download_command": (
        "huggingface-cli download stabilityai/TripoSR "
        "--local-dir models/triposr --include config.yaml model.ckpt"
    ),
    "upstream_install_command": (
        "git clone https://github.com/VAST-AI-Research/TripoSR && "
        "pip install -e TripoSR   # 需要 torch、torchmcubes、transformers==4.35.0、rembg"
    ),
    "verification_status": "CODE_READY_MODEL_UNTESTED",
}


def model_card() -> dict:
    """Return a deep copy so callers cannot mutate the manifest."""
    return copy.deepcopy(MODEL_CARD)


def public_card() -> dict:
    card = model_card()
    card["weights_bundled"] = False
    card["weights_in_repo"] = False
    return card
