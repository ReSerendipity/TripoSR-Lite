# 模型清单与许可（TripoSR-Lite）

最后核查：2026-11-25（含本机真实推理实测）。

## 1. 默认模型：TripoSR

| 项 | 内容 |
|---|---|
| 官方代码 | https://github.com/VAST-AI-Research/TripoSR |
| 代码许可 | **MIT**（仓库 `LICENSE`；README 明示覆盖源码、预训练权重与在线 Demo） |
| 官方权重 | https://huggingface.co/stabilityai/TripoSR |
| 权重许可 | **MIT**（模型卡 `License: mit`） |
| 权重文件 | `model.ckpt` **1.68 GB（1.56 GiB）**；`config.yaml` **987 B** |
| 训练数据 | Objaverse 精选子集，**CC-BY**（模型卡原文） |
| 官方显存 | 单图默认参数约 **6 GB**（README 官方声明，未在本机实测） |
| 官方速度 | A100 上前向 < 0.5 s（官方声明） |
| 输入分辨率 | 512×512（`cond_image_size: 512`） |
| 输出网格 | Triplane NeRF + marching cubes，默认分辨率 256、chunk 8192，带顶点色 |
| 本仓库分发 | **否**（权重不入库、不随安装包分发） |
| 本机验证 | **`MODEL_TESTED`**（2026-11-25，RTX 5070 Ti Laptop 12 GB）：官方 `run.py`，512×512，mc 256，fp32，batch 1；峰值显存 3,236 MiB，前向 2.69 s，端到端 15.89 s；42,076 顶点 / 84,156 面，watertight。详见 `reports/triposr/phase-c.md` |

### 下载

```powershell
pip install "huggingface_hub"
python -c "from huggingface_hub import snapshot_download; snapshot_download('stabilityai/TripoSR', local_dir='models/triposr', allow_patterns=['config.yaml','model.ckpt'])"
```

本机实测（2026-11-25）：

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `model.ckpt` | 1,677,246,742 | `429e2c6b22a0923967459de24d67f05962b235f79cde6b032aa7ed2ffcd970ee` |
| `config.yaml` | 987 | `74ca708ce086bf68e97709ea6b3d91f14717921c04691e84043f0eb8fcc68e62` |

- 直连 `huggingface.co` 在本机 TLS 校验失败（`CERTIFICATE_VERIFY_FAILED`），改 `HF_ENDPOINT=https://hf-mirror.com` 后 64 s 下完，27–40 MB/s。
- `snapshot_download(local_dir=...)` 默认留符号链接（`model.ckpt → ~/.cache/huggingface/.../blobs/429e…`）。要让 `models/triposr` 自包含，需把 blob 复制成真实文件；本机已复制并复核 SHA256。
- 服务会把小于 1 GB 的文件判为 `WEIGHT_TOO_SMALL`（阈值可用 `TRIPOSR_MIN_CKPT_BYTES` 调整）。

### 上游代码安装（真实推理必需）

```powershell
git clone --depth 1 https://github.com/VAST-AI-Research/TripoSR external/TripoSR
# external/TripoSR/.venv：Python 3.12.10，复用本机 torch 2.11.0+cu128（未重下 wheel）
pip install "transformers==4.36.2" "rembg" "trimesh>=4.5" "scikit-image" "omegaconf==2.3.0" "einops==0.7.0" "huggingface_hub==0.20.3" "xatlas==0.0.9" "moderngl==5.10.0"
```

- 上游钉死的 `transformers==4.35.0` 要求 `tokenizers<0.15`，0.14.x 无 cp312 wheel，Python 3.12 上无法安装；实测用 **4.36.2**（`pip check` 通过，DINOv2 视觉塔行为不变）。
- `trimesh==4.0.5` 在 numpy 2.x 下 GLB 导出崩溃（`ndarray.ptp` 已移除），实测用 **5.2.2**。
- `torchmcubes` CUDA 扩展**未编译成功**（本机无 CUDA Toolkit/nvcc，CMake 卡在 torch `Caffe2Config.cmake` 的 CUDA 库检查）。网格提取改走 CPU：`scripts/_compat/torchmcubes` 经 `PYTHONPATH` 提供 `torchmcubes.marching_cubes`（scikit-image Lewiner），上游 `run.py` 不改；前向仍是真实 GPU 推理。
- 纹理烘焙额外需要 `xatlas`、`moderngl`（已安装），首版仍未启用。

## 2. 附带权重（自动下载，不随本仓库分发）

| 组件 | 用途 | 许可 | 体积 | 触发时机 |
|---|---|---|---|---|
| `facebook/dino-vitb16`（DINOv2 ViT-B/16） | TripoSR 图像 tokenizer | **Apache-2.0** | **454 B**（仅 `config.json`；权重在 TripoSR ckpt 内） | 首次 `TSR.from_pretrained` |
| `rembg` / u2net | 推理前抠图 | rembg **MIT**；u2net 权重来自 U-2-Net，**Apache-2.0** | **175,997,641 B**（MD5 `60024c5c889badc19c04ad937298a77b`） | 首次真实推理且未跳过抠图 |

两者都只在真实推理路径上出现；DEMO 与测试路径不下载。本机 `u2net.onnx` 因 GitHub 直连 TLS 被代理拦截，用 `curl --ssl-no-revoke` 下到 `~/.u2net/`，MD5 与 rembg 内置校验值一致。

## 3. 许可边界

- 本项目外壳代码：**Apache-2.0**。
- 上游 TripoSR 代码与权重仍是 **MIT**，不因外壳而改变；本项目不复制其源码。
- Objaverse 的 CC-BY 署名保留在 `NOTICE`。
- 模型卡的使用限制（不得生成令人不适、传播刻板印象的内容）由使用者自行遵守。

## 4. 未执行项

- 应用内推理链路：R4-B 已通过真实 GLB 浏览器闭环；R5-A 增加 OBJ 下载分支与失败分支证据，真实模型下的 OBJ 下载待 R5-B 复跑。项目 `.venv` 仍不安装 torch/`tsr`。
- CUDA 版 `torchmcubes`：本机无 CUDA Toolkit，未编译；CPU（scikit-image）与 CUDA 结果未做逐位比对。
- 多图 batch、`mc-resolution=512`、`--bake-texture` 纹理烘焙、`--render` NeRF 视频：未测。
- 8 GB / 16 GB / 24 GB 其他显卡：未测。
