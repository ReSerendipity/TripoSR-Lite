# Third-party notices — MeshMint-Lite

Shell code licence: Apache-2.0 (`LICENSE`). The table below separates **code**
licences from **weight** licences; weights are never redistributed here.

| Component | Role | Code licence | Weight licence | Distributed in repo | Source |
|---|---|---|---|---|---|
| TripoSR | single-image → 3D mesh model | MIT | MIT | No (upstream clone by user) | https://github.com/VAST-AI-Research/TripoSR |
| TripoSR weights (`model.ckpt`, `config.yaml`) | pretrained checkpoint | — | MIT | No | https://huggingface.co/stabilityai/TripoSR |
| Objaverse (curated subset) | TripoSR training data | — | CC-BY | No | https://objaverse.allenai.org/ |
| DINOv2 ViT-B/16 (`facebook/dino-vitb16`) | image tokenizer inside TripoSR | Apache-2.0 | Apache-2.0 | No (auto-download) | https://huggingface.co/facebook/dino-vitb16 |
| rembg | background removal before inference | MIT | — | No (pip dependency) | https://github.com/danielgatis/rembg |
| U-2-Net / u2net weights | rembg segmentation weights | Apache-2.0 | Apache-2.0 | No (auto-download) | https://github.com/xuebinqin/U-2-Net |
| Three.js r180 | browser 3D preview | MIT | — | Yes (vendored build) | https://threejs.org/ |
| FastAPI / Starlette / Uvicorn | local HTTP service | MIT / BSD-3-Clause | — | No (pip dependency) | https://fastapi.tiangolo.com/ |
| trimesh | mesh container + OBJ/GLB export | MIT | — | No (pip dependency) | https://trimesh.org/ |
| Pillow | image decode / validation | HPND (PIL licence) | — | No (pip dependency) | https://python-pillow.org/ |
| NumPy | array math | BSD-3-Clause | — | No (pip dependency) | https://numpy.org/ |

## Notes

- The MIT licence of TripoSR explicitly covers source code, pretrained models and
  the online demo (upstream README).
- Objaverse attribution is preserved in `NOTICE`; no dataset content is shipped.
- Three.js build files retain their SPDX headers; full text in
  `THIRD_PARTY_LICENSES/three-LICENSE.txt`.
- `rembg` and `facebook/dino-vitb16` are only fetched when a user actually runs
  real inference; this repository's tests and demo path never download them.
