# TripoSR-Lite — Phase C：真实单图生成 3D（R2）

- 项目：`C:/Users/Doro/APP/repository/TripoSR-Lite`
- 执行日期：2026-11-25（UTC+8）
- 执行者：豆包（DeepSeek V4.1 Flash，未改模型）
- 结论：**`MODEL_TESTED`**（范围：上游官方 `run.py` + `stabilityai/TripoSR` 真实权重，512×512，mc=256，fp32，batch 1，单张官方示例图；网格提取为 CPU 替换点）

## 1. 前置闸门与断点

| 项 | 状态 | 处置 |
|---|---|---|
| Layered-Lite GPU | 已结束，`nvidia-smi` 无 compute app | 满足前置闸门后开始 |
| GPU 空闲 | 12,227 MiB 总，**已用 0 MiB（空闲 11,944 MiB）** | ≥7 GB 闸门通过 |
| `external/TripoSR` | 不存在 | `git clone --depth 1`，HEAD `107cefdc244c39106fa830359024f6a2f1c78871`（2026-06-04） |
| `models/triposr` | 不存在 | 新下载，未产生第二份完整权重目录 |
| 现有 `torch` | 本机 `Subtitle-Lite/.venv` 有 torch 2.11.0+cu128 | 复制进 TripoSR venv，**未重新下载 2.75 GB wheel** |

## 2. 权重：来源、字节数、哈希

- 下载工具：`huggingface_hub.snapshot_download`（`allow_patterns=['config.yaml','model.ckpt']`，`max_workers=4`）
- endpoint：`https://huggingface.co` 首次失败（`CERTIFICATE_VERIFY_FAILED`，本机 TLS 被代理链拦截），自动回退 **`https://hf-mirror.com`** 成功；64 s 完成，27–40 MB/s。实际 endpoint 写入 `models/triposr/weights-manifest.json`。
- 访问/下载日期：2026-11-25

| 文件 | 字节 | SHA256 |
|---|---:|---|
| `models/triposr/model.ckpt` | 1,677,246,742 | `429e2c6b22a0923967459de24d67f05962b235f79cde6b032aa7ed2ffcd970ee` |
| `models/triposr/config.yaml` | 987 | `74ca708ce086bf68e97709ea6b3d91f14717921c04691e84043f0eb8fcc68e62` |
| `~/.u2net/u2net.onnx`（rembg） | 175,997,641 | `8d10d2f3bb75ae3b6d527c77944fc5e7dcd94b29809d47a739a7a728a912b491`（MD5 `60024c5c889badc19c04ad937298a77b`，与 rembg 内置校验值一致） |
| `~/.cache/huggingface/.../dino-vitb16/config.json` | 454 | `b87c0270b97db085fd82cf114a761fd0f62ae7914fbd407c752a2260646b689c` |

处置细节：

1. `snapshot_download(local_dir=...)` 在 `models/triposr/` 生成的是**符号链接**（`model.ckpt → ~/.cache/huggingface/.../blobs/429e…`）。为避免权重依赖缓存目录，已删除链接并把 blob 复制为真实文件，复制后重算 SHA256 与 manifest/Hub LFS oid 一致。
2. **DINOv2 只下载了 `config.json`（454 B）**：TripoSR 的 `DINOSingleImageTokenizer` 以 `ViTModel(config)` 构建视觉塔，权重本身包含在 `model.ckpt` 的 state_dict 中，不存在单独的 0.33 GB DINOv2 权重下载。这与工单「允许首次推理下载 DINOv2」一致，实际未触发大文件。
3. u2net：GitHub release 直连同样被 TLS 拦截，改用系统 `curl --ssl-no-revoke` 下载（GitHub 302 → 对象存储），落到 rembg 默认路径 `~/.u2net/u2net.onnx`；rembg 启动时用内置 MD5 复验并复用。
4. 未下载任何其他模型（无 SAM、无 Qwen-Image-Layered、无其他 TripoSR 变体）。

## 3. 环境与兼容性

隔离环境：`external/TripoSR/.venv`（Python **3.12.10**；本机无 3.10/3.11，按工单记录 3.12 兼容尝试）。

| 组件 | 版本 | 说明 |
|---|---|---|
| torch | **2.11.0+cu128** | 从 `C:/Users/Doro/APP/repository/Subtitle-Lite/.venv` 复制（含 torchgen/functorch/sympy 等依赖），未下载 wheel |
| transformers | **4.36.2** | 上游钉死 4.35.0，但 4.35.0 要求 `tokenizers<0.15`，0.14.x 无 cp312 wheel；实测 4.35.0 导入即拒绝 tokenizers 0.15.2 |
| tokenizers | 0.15.2 | 与 4.36.2 兼容 |
| huggingface_hub | 0.20.3 | 与 4.36.x 兼容，支持 `snapshot_download` |
| trimesh | **5.2.2** | 上游钉死 4.0.5 在 numpy 2 下 GLB 导出崩溃（`ndarray.ptp` 已移除） |
| numpy / scikit-image | 2.1.3 / 0.26.0 | CPU marching cubes 后端 |
| rembg / onnxruntime | 2.0.69 / 1.32.0 | u2net 抠图 |
| 其他 | omegaconf 2.3.0、einops 0.7.0、xatlas 0.0.9、moderngl 5.10.0、Pillow 13.0.0 | 与上游 requirements 对齐 |
| setuptools | 81.0.0 | torch 2.11 要求 `<82` |

`pip check`：**No broken requirements found.（exit 0）**。

`torchmcubes` 编译尝试与结论：

1. `pip install git+https://github.com/tatsy/torchmcubes.git`（默认 build isolation）→ `RuntimeError: torchmcubes must be built against an installed PyTorch, but the 'torch' package was not found in the build environment`。
2. 按 README 改 `--no-build-isolation`，在 `vcvars64.bat` 子进程内配置 MSVC 18 Build Tools（`cl.exe` 14.51.36231）+ CMake 4.4.5 + ninja，并显式给 `CMAKE_PREFIX_PATH`（torch / pybind11）→ CMake 通过 pybind11 检查后卡在 torch 的 `Caffe2Config.cmake:90`：
   `Your installed Caffe2 version uses CUDA but I cannot find the CUDA libraries.`
   根因：本机**没有 CUDA Toolkit / nvcc**（`torch.utils.cpp_extension.CUDA_HOME = None`），而本机 torch 是 CUDA 构建，`find_package(Torch)` 强制要求 CUDA 库；`check_language(CUDA)` 也失败。
3. 结论：CUDA extension 不可编译；按工单回退「保留真实前向 + CPU 提取网格」。

替换点（唯一）：`tsr/models/isosurface.py` 的 `from torchmcubes import marching_cubes`。
实现：`scripts/_compat/torchmcubes/__init__.py` 经 `PYTHONPATH` 注入，提供同名 `marching_cubes(vol, thresh)`，内部用 `skimage.measure.marching_cubes(method='lewiner', allow_degenerate=False)`，并复刻 torchmcubes 的轴序约定（返回 `(k,j,i)`，上游再 `[2,1,0]` 还原）。上游 `run.py` 与 `tsr/` 源码**一行未改**；DINOv2 编码、Transformer backbone、triplane 解码、密度/颜色查询全部是真实模型在 CUDA 上执行。

轴序/几何正确性单独用解析球验证（64³，球心 (40,20,32)、R=12）：shim 原始顶点均值 `(32,20,40)`，经上游 `[2,1,0]/(N-1)` 后为 `(0.6349,0.3175,0.5079)`，与期望 `(40,20,32)/63` 完全一致。

## 4. 推理命令与参数

入口：`scripts/run_triposr_real.py`（监督脚本：显存采样、日志、退出码、导出校验；实际推理由官方 `external/TripoSR/run.py` 子进程执行）

```powershell
cd C:\Users\Doro\APP\repository\TripoSR-Lite
external\TripoSR\.venv\Scripts\python.exe scripts\run_triposr_real.py `
  --image external\TripoSR\examples\chair.png `
  --output-dir results\real-20261125-101344 `
  --mc-resolution 256 --chunk-size 8192
```

上游 argv：

```
<project>\results\real-20261125-101344\input.png
--pretrained-model-name-or-path <project>\models\triposr
--output-dir <project>\results\real-20261125-101344\upstream
--mc-resolution 256 --chunk-size 8192 --device cuda:0 --model-save-format glb
```

- 模型版本：`stabilityai/TripoSR`（`model.ckpt` 哈希见 §2），fp32，batch 1
- 输入：上游官方示例 `examples/chair.png`（512×512 RGBA，114,705 B，SHA256 `2503c12a…b5bf`），默认 rembg 抠图 + `resize_foreground(0.85)`，`cond_image_size=512`
- `mc-resolution=256`，`chunk-size=8192`，seed 不适用（确定性前向，无采样）
- 环境变量：`PYTHONPATH=<project>\scripts\_compat`、`HF_ENDPOINT=https://hf-mirror.com`、`HF_HUB_DISABLE_XET=1`

## 5. 指标

来源：`results/real-20261125-101344/metrics.json`（`nvidia-smi` 0.5 s 采样 25 点；上游日志计时）

| 指标 | 值 |
|---|---|
| 退出码 | 0 |
| 端到端（监督脚本） | 18.618 s |
| 上游进程 | 15.887 s |
| 模型加载 | 4.992 s |
| 抠图 + 缩放 | 0.668 s |
| 前向（`model([image])`） | 2.694 s |
| 网格提取（CPU，mc 256） | 2.841 s |
| 导出 GLB | 0.007 s |
| 峰值显存（采样） | **3,236 MiB** |
| 加载前空闲显存 | 11,944 MiB（12,227 总，0 已用） |

说明：同一环境更早一次尝试（10:12:39，因 trimesh 兼容问题失败）记录到前向 9.77 s / 提取 5.82 s，为首次 CUDA 预热开销；本报告以上表为最终口径。

## 6. 输出与导出复核

目录 `results/real-20261125-101344/`：`input.png`、`upstream/0/input.png`（rembg 抠图结果）、`upstream/0/mesh.glb`、`mesh.glb`、`mesh.obj`、`metrics.json`、`run.log`、`gpu-before.json`、`gpu-after.json`、`preview.png`（三视图诊断投影）。

| 检查 | GLB | OBJ |
|---|---|---|
| 字节 | 1,684,080 | 4,544,198 |
| 文件头 | `glTF`（二进制 glTF 2.0 魔数） | `# https://github.com/mikedh/trimesh` |
| 顶点 / 面 | 42,076 / 84,156 | 42,076 / 84,156（trimesh 回读一致） |
| watertight | ✔ | — |
| winding 一致 | ✔ | — |
| 顶点色 | ✔（`COLOR_0`） | ✔（`v x y z r g b` 扩展色） |
| 包围盒 | min `(-0.3103,-0.2805,-0.4967)`，max `(0.2934,0.2881,0.5695)`，半径内 | 同 |
| 体积 | 0.064517（正） | — |

`preview.png` 三视图可见完整椅子（座面、四腿、靠背、软包颜色），非退化网格。

失败留痕（保留，不删）：

- `results/real-20261125-101031/`：u2net 下载被 TLS 拦截，`rembg.new_session()` 抛 `SSLError`，exit 1。
- `results/real-20261125-101239/`：推理与提取成功（前向 9.77 s、提取 5.82 s），GLB 导出阶段 trimesh 4.0.5 + numpy 2 的 `ndarray.ptp` 报错，exit 1；该次已留 `metrics.json` 与完整日志。

## 7. 未测项

- 应用内推理链路（FastAPI → `triposr_lite/adapter.py` → `tsr`）：项目 `.venv`（3.12.13）没有 torch/`tsr`，`/api/model` 仍 `NOT_READY`；本轮只验证官方 CLI 路径。
- CUDA marching cubes：本机无 CUDA Toolkit，未编译，未与 CPU 结果做逐位/拓扑比对。
- `mc-resolution=512`、`--bake-texture`（xatlas/moderngl 已安装但未用）、`--render` NeRF 视频、多图 batch。
- 其他输入图（人物、薄壁、透明材质）质量未评估。
- 8 GB / 16 GB / 24 GB 显卡与不同驱动版本。

## 8. 变更与副作用

- 新增 `scripts/run_triposr_real.py`（真实推理监督 + 显存/耗时/导出校验）
- 新增 `scripts/_compat/torchmcubes/__init__.py`（CPU marching cubes 兼容垫片，唯一替换点）
- 新增 `reports/triposr/phase-c.md`（本文件）
- 更新 `README.md`（状态、真实环境、实测指标、已知限制）、`docs/MODELS.md`（状态、权重哈希、未测项）
- `.gitignore` 增加 `external/`、`results/`（上游 checkout 与运行产物不入库）
- `pip check` 通过；`pytest -q` **44 passed**（exit 0）
- 未改上游 `external/TripoSR` 源码；未改全局 Python/torch/CUDA/git config；未改其他项目
- 未 push、未建 PR/Issue/Release/tag、未注册计划任务
- 磁盘：torch 复制约 4.2 GB + 依赖约 1.5 GB + 权重 1.68 GB；C 盘空闲 50.8 GB → 42.2 GB

## 9. 仓库状态

- 提交前 HEAD：`bccb588`（`TripoSR-Lite: 品牌名由 MeshMint(-Lite) 修正为 TripoSR(-Lite)…`）
- 本轮只新增一笔 DCO 提交（`git commit -s`），不改写旧提交；提交后工作树干净
- 无远程（`git remote -v` 为空）；`models/`、`external/`、`results/` 由 `.gitignore` 忽略
