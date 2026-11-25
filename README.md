# TripoSR-Lite

本地单图生成 3D 网格的工作台：导入一张 PNG/JPEG，按需加载 [TripoSR](https://github.com/VAST-AI-Research/TripoSR)，在 Three.js 里预览，导出 OBJ / GLB。默认只监听 `127.0.0.1`，不上传图片、不调用云端 API。

**当前验证状态：`MODEL_TESTED`（2026-11-25，RTX 5070 Ti Laptop 12 GB）** —— 官方 `run.py` + `stabilityai/TripoSR` 权重在本机完成真实单图 → 网格推理，导出 OBJ/GLB（42,076 顶点 / 84,156 面，watertight），证据见 [`reports/triposr/phase-c.md`](reports/triposr/phase-c.md)。

注意：工作台自身的 `.venv`（Python 3.12.13）仍未安装 torch / 上游 `tsr`，但应用进程已不再 `import torch`：就绪探测改为在隔离运行时 `external/TripoSR/.venv` 里做短超时 `torch/tsr` 探测，推理则调用监督脚本子进程。R4-B 已完成真实 GLB 浏览器闭环；R5-A 补齐 OBJ 下载校验与失败分支证据（本阶段不加载模型、不下载权重）。未就绪时界面显示 `NOT_READY`，预览区展示明确标注 `DEMO` 的程序生成占位网格，不冒充推理结果。

## 边界

- 只做「单图 → 网格」：不做 3D 高斯、场景重建、贴图烘焙、云端生成。
- 与 `Scan-Memories` 的分工：本项目负责生成与导出；`Scan-Memories` 负责浏览既有 GLB/PLY 扫描。本项目不读取、不修改 `Scan-Memories`。

## 功能

1. 导入 PNG/JPEG，显示原图，并给出**粗略**前景占比提示（四角背景色差或 alpha 通道；不是分割模型）。
2. 按需加载 TripoSR，按阶段显示进度（加载模型 / 抠图缩放 / 前向重建 / 提取网格），支持取消与超时。
3. 本地 Three.js 预览：旋转、缩放、平移、复位视角、线框切换。
4. 导出 OBJ（默认必备）与 GLB（trimesh 原生导出；只含顶点色，不含烘焙贴图）。
5. 无权重时 `NOT_READY` + `DEMO` 演示网格；导出文件名与文件头都带 DEMO 标记。
6. 输入校验（格式、损坏文件、体积、分辨率）与机器可读错误码。

## 快速开始

```powershell
cd C:\Users\Doro\APP\repository\TripoSR-Lite
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m triposr_lite serve --host 127.0.0.1 --port 7861
```

打开 <http://127.0.0.1:7861>。也可直接双击 `start-local.cmd`（首次会自动建 venv 并装依赖）。

只查看模型状态（不下载权重、不启动服务）：

```powershell
python -m triposr_lite probe
```

## 模型下载（本仓库不含权重）

```powershell
pip install "huggingface_hub"
python -c "from huggingface_hub import snapshot_download; snapshot_download('stabilityai/TripoSR', local_dir='models/triposr', allow_patterns=['config.yaml','model.ckpt'])"
```

- `models/triposr/model.ckpt`：1,677,246,742 B（1.56 GiB），SHA256 `429e2c6b22a0923967459de24d67f05962b235f79cde6b032aa7ed2ffcd970ee`，MIT。
- `models/triposr/config.yaml`：987 B，SHA256 `74ca708ce086bf68e97709ea6b3d91f14717921c04691e84043f0eb8fcc68e62`，MIT。
- 2026-11-25 实测：直连 `huggingface.co` 在本机 TLS 校验失败，改用 `HF_ENDPOINT=https://hf-mirror.com` 下载成功（约 1 分钟，27–40 MB/s）。实际 endpoint 记录在 `models/triposr/weights-manifest.json`。
- `snapshot_download(local_dir=...)` 会在 `models/triposr/` 留下指向 `~/.cache/huggingface` 的**符号链接**；要让目录自包含，需把 blob 复制成真实文件（本机已这样做）。
- `transformers` 只会拉 `facebook/dino-vitb16` 的 `config.json`（454 B，Apache-2.0）——DINOv2 权重就在 TripoSR 的 `model.ckpt` state_dict 里，不会单独下载 0.33 GB。
- `rembg` 需要 `u2net.onnx`（175,997,641 B，MD5 `60024c5c889badc19c04ad937298a77b`，与 rembg 内置校验值一致）。本机 GitHub 直连被 TLS 代理拦截，改用 `curl --ssl-no-revoke` 下到 `~/.u2net/u2net.onnx` 后 rembg 直接复用。
- 目录可用 `TRIPOSR_MODEL_DIR` 指向别处；权重不入库、不随安装包分发。

真实推理实测环境（2026-11-25，`MODEL_TESTED`）：`external/TripoSR/.venv`，Python **3.12.10**，torch **2.11.0+cu128**（从本机已有安装复制进来，未重新下载 2.75 GB wheel）：

```powershell
git clone --depth 1 https://github.com/VAST-AI-Research/TripoSR external/TripoSR
# 建 .venv 后复用本机 torch，再装其余依赖
pip install "transformers==4.36.2" "rembg" "trimesh>=4.5" "scikit-image" "omegaconf==2.3.0" "einops==0.7.0" "huggingface_hub==0.20.3" "xatlas==0.0.9" "moderngl==5.10.0"
```

上游钉死的 `transformers==4.35.0` 在 Python 3.12 上装不出来：它要求 `tokenizers<0.15`，而 0.14.x 没有 cp312 wheel；实测 4.35.0 一导入就拒绝 0.15.2。改用 **4.36.2**（同样只用到 DINOv2 视觉塔），`pip check` 通过。`trimesh==4.0.5` 与 numpy 2 不兼容（GLB 导出时 `ndarray.ptp` 报错），实测升到 **5.2.2**。

`torchmcubes` 的 CUDA 扩展在本机**编译失败**：没有 CUDA Toolkit/nvcc，CMake 在 torch 的 `Caffe2Config.cmake` 处报 "installed Caffe2 uses CUDA but I cannot find the CUDA libraries"。因此网格提取改走 CPU：`scripts/_compat/torchmcubes` 通过 `PYTHONPATH` 提供 `torchmcubes.marching_cubes`（scikit-image 实现），**上游 `run.py` 一行未改**；DINOv2、Transformer、triplane 解码与密度/颜色查询仍是真实模型在 GPU 上跑。

实测命令（官方 `run.py` + 监督脚本记录显存/耗时）：

```powershell
external\TripoSR\.venv\Scripts\python.exe scripts\run_triposr_real.py `
  --image external\TripoSR\examples\chair.png `
  --output-dir results\real-20261125-101344 --mc-resolution 256 --chunk-size 8192
```

## 应用内推理链路（R4-A）

- `adapter.py` 用 `TRIPOSR_RUNTIME_PYTHON`（默认 `external/TripoSR/.venv/Scripts/python.exe`）执行 `scripts/run_triposr_real.py`，参数为数组、`shell=False`；`PYTHONPATH` 的 CPU marching-cubes 垫片由监督脚本自己设置。
- 每个任务在 `results/app-<YYYYMMDD-HHMMSS>-<uuid>/` 下留全套证据：`input.png`、`request.json`、`run.log`、`supervisor.log`、`gpu-before/after.json`、`metrics.json`、`mesh.glb/.obj`；失败或取消同样保留。
- 进度来自 `run.log` 的上游阶段行（`Initializing model` / `Processing images` / `Running model` / `Extracting mesh`），不是计时假进度。
- 取消/超时会终止整个子进程树；真实运行前仍由监督脚本执行「无其他计算进程 + 空闲显存 ≥ `TRIPOSR_MIN_FREE_MIB`（默认 7168 MiB）」硬闸门，`GPU_BUSY` / `INSUFFICIENT_VRAM` 直接失败，不降级。
- 可用环境变量覆盖：`TRIPOSR_UPSTREAM_DIR`、`TRIPOSR_RUNTIME_PYTHON`、`TRIPOSR_SUPERVISOR`、`TRIPOSR_RESULTS_DIR`、`TRIPOSR_MIN_FREE_MIB`、`TRIPOSR_DEVICE`、`TRIPOSR_PROBE_TIMEOUT_S`、`TRIPOSR_PROBE_TTL_S`。

## 浏览器验收（R4-B / R5-A）

`scripts/browser_real.py` 用系统 Edge（Playwright，headless 1440×1000）跑页面闭环。准备阶段只读 NOT_READY 页面，不触发任务：

```powershell
# 终端 A：未就绪开发模式（指向不含权重的目录即可）
$env:TRIPOSR_MODEL_DIR = "$PWD\models\triposr-notready"
.\.venv\Scripts\python.exe -m uvicorn triposr_lite.app:app --host 127.0.0.1 --port 8876

# 终端 B：验收（解释器需带 playwright；本机借用 VOSR2-Flash 的 .venv）
python scripts\browser_real.py --base-url http://127.0.0.1:8876 `
  --input external/TripoSR/examples/chair.png --prepare-only `
  --output-dir results\browser-prep
```

真实阶段要求 `/api/model` 为 READY，会点击 `#exportObjBtn` / `#exportGlbBtn`：

```powershell
python scripts\browser_real.py --base-url http://127.0.0.1:8876 `
  --input external/TripoSR/examples/chair.png --format both `
  --output-dir results\browser-real --timeout 900
```

- `--format obj|glb|both`（默认 both）。OBJ 校验：首行 `# TripoSR-Lite OBJ export`、`# source: model`、标签与对象名不含 `DEMO`、`v` / `f` 计数非空且与页面统计一致、响应头 `X-TripoSR-Demo: false`。
- 任务失败或下载失败时仍写 `acceptance.json`、`metrics.json`、`network.json`、`console.log`、`pageerror.log`；`acceptance.json` 的 `flow` / `job` / `error` 记录失败原因。

## 显存与性能（本机实测，2026-11-25）

单张 `examples/chair.png`（512×512）、`mc-resolution=256`、`chunk-size=8192`、fp32、batch 1：

| 项 | 值 | 备注 |
|---|---|---|
| GPU | RTX 5070 Ti Laptop，12,227 MiB | 加载前空闲 11,944 MiB，无其他 compute app |
| 峰值显存 | **3,236 MiB**（nvidia-smi 0.5 s 采样，25 点） | 远低于官方「约 6 GB」口径 |
| 模型加载 | 4.99 s | 含 DINOv2 `config.json` + `model.ckpt` 反序列化 |
| 抠图 + 缩放 | 0.67 s | rembg u2net，512×512 |
| 前向（triplane 解码） | 2.69 s | 冷启动那次为 9.77 s |
| 网格提取 | 2.84 s | **CPU** scikit-image marching cubes，mc 256 |
| 导出 | <0.01 s（GLB） | 之后用 trimesh 转出 OBJ |
| 端到端 | 15.89 s 上游进程 / 18.62 s 含监督 | 退出码 0 |
| 输出 | 42,076 顶点 / 84,156 面 | GLB 1,684,080 B；OBJ 4,544,198 B；watertight、winding 一致、带顶点色 |

## 导出

- 下面描述的是**工作台自身**的导出格式；上游 `run.py` 实测导出的是 trimesh 原生文件（OBJ 头 `# https://github.com/mikedh/trimesh`、GLB 为 glTF 2.0），同样带顶点色。
- `OBJ`：`# TripoSR-Lite OBJ export` 文件头，含 source / label / 顶点面数、`v`（带 RGB 扩展色）、`vn`、`f v//vn`。
- `GLB`：标准 glTF 2.0 二进制容器；节点名 `DEMO_NON_INFERENCE`（演示）或 `TRIPOSR_MESH`（推理结果）。
- DEMO 导出的文件名固定为 `triposr_lite-demo.obj/.glb`，响应头带 `X-TripoSR-Demo: true`。
- 当前版本不烘焙贴图（上游 `--bake-texture` 依赖 xatlas，未纳入首版）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查 |
| GET | `/api/model` | 就绪探测 + 模型清单（URL / 许可 / 体积 / 下载命令） |
| GET | `/api/demo` | DEMO 演示网格 |
| POST | `/api/inspect` | 校验图片 + 粗略前景提示（multipart `file`） |
| POST | `/api/jobs` | 创建任务；未就绪返回 `NOT_READY` + DEMO 网格 |
| GET | `/api/jobs/{id}` | 任务状态、进度、网格 |
| POST | `/api/jobs/{id}/cancel` | 请求取消；子进程运行中会终止整个进程树 |
| GET | `/api/jobs/{id}/mesh` | 网格 JSON（positions / indices / colors / stats） |
| GET | `/api/jobs/{id}/export?format=obj\|glb` | 导出文件 |

任务状态：`QUEUED`、`RUNNING`、`NOT_READY`、`DONE`、`FAILED`、`TIMEOUT`、`CANCEL_REQUESTED`、`CANCELLED`。

## 测试

```powershell
pip install -r requirements-dev.txt
python -m pytest tests -q
```

覆盖输入校验、前景提示、模型探测、任务状态机（取消 / 超时 / 失败）、DEMO 分支、OBJ/GLB 结构与回读、API 端到端、失败分支证据（`termination.json` / 不写 mesh / 409 导出）以及浏览器脚本的 OBJ 头解析与格式选择。测试不需要 GPU、不需要权重，也不联网；当前 **83 项**全部通过（2026-11-25）。

## 许可

- 本项目外壳代码：**Apache-2.0**（见 `LICENSE`）。
- TripoSR 代码与权重：**MIT**；训练数据 Objaverse 精选子集 **CC-BY**。
- DINOv2 ViT-B/16（`facebook/dino-vitb16`）：**Apache-2.0**。
- rembg：**MIT**；u2net 权重来自 U-2-Net（**Apache-2.0**）。
- Three.js：**MIT**。
- 逐项说明见 [`docs/MODELS.md`](docs/MODELS.md)、[`NOTICE`](NOTICE)、[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
- 本仓库**不包含**任何模型权重；权重许可不因本项目而改变。

## 已知限制

1. 真实浏览器闭环已在 R4-B 通过（GLB）；R5-A 增加 OBJ 下载分支，但**真实模型下的 OBJ 下载尚未复跑**（计划 R5-B）。`adapter.py` 不在应用进程 import torch，探测与推理都在 `TRIPOSR_RUNTIME_PYTHON`（默认 `external/TripoSR/.venv/Scripts/python.exe`）里执行；`verification_status` 仍保守保留为 `CODE_READY_MODEL_UNTESTED`。
2. Python 3.12 与上游钉死的 `transformers==4.35.0` 不兼容（见上），实测用 4.36.2；`trimesh==4.0.5` 与 numpy 2 不兼容，实测用 5.2.2。都属于依赖层兼容修正，未改 TripoSR 逻辑。
3. 本机没有 CUDA Toolkit，`torchmcubes` 只能走 CPU 提取；与 CUDA 版 marching cubes 未做逐位比对，拓扑结果来自 scikit-image（Lewiner 算法）。
4. 取消/超时依赖终止监督脚本子进程树（Windows `taskkill /T`），不是 GPU 前向抢占：信号到达后有最多约 0.2s 轮询延迟与进程退出等待。
5. 网格只驻留内存，重启服务即释放；大网格（mc 256）预览 JSON 体积较大，未做简化。
6. 前景提示只是颜色/alpha 统计，不等于 rembg 的抠图结果。
7. 生成质量受输入影响大：单主体、清晰、背景干净更稳；不承诺任意图片都能得到好模型。

## 未测边界（R5-A 后）

- 真实模型下的 OBJ 浏览器下载未复跑（R5-A 仅跑 `--prepare-only` 与 fake supervisor 单测）。
- 取消、超时、GPU busy、显存不足仍为 fake supervisor / 假进程单测；真实推理下未演练（留待 R5-B 串行 GPU，启动前重测显存与计算进程）。
- 仅 `examples/chair.png` 单样本；其他主体、分辨率、JPEG 输入与极端前景占比未覆盖。
- CPU 推理路径、多任务并发、`MESH_EVICTED` 未覆盖；浏览器仅 Edge headless 1440×1000。
