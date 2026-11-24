# TripoSR-Lite

本地单图生成 3D 网格的工作台：导入一张 PNG/JPEG，按需加载 [TripoSR](https://github.com/VAST-AI-Research/TripoSR)，在 Three.js 里预览，导出 OBJ / GLB。默认只监听 `127.0.0.1`，不上传图片、不调用云端 API。

**当前验证状态：`CODE_READY_MODEL_UNTESTED`** —— 源码、自动化测试、开发模式冒烟与导出格式已完成；真实 TripoSR 推理**未执行**（本轮未下载权重）。未就绪时界面显示 `NOT_READY`，预览区展示明确标注 `DEMO` 的程序生成占位网格，不冒充推理结果。

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
pip install "huggingface_hub[cli]"
huggingface-cli download stabilityai/TripoSR --local-dir models/triposr --include config.yaml model.ckpt
```

- `models/triposr/model.ckpt`：1.68 GB（1.56 GiB），MIT。
- `models/triposr/config.yaml`：987 B，MIT。
- 首次真实推理时 `transformers` 还会下载 `facebook/dino-vitb16`（约 0.33 GB，Apache-2.0）；`rembg` 会下载 `u2net`（约 176 MB）。磁盘建议预留 2.5 GB。
- 目录可用 `TRIPOSR_MODEL_DIR` 指向别处；权重不入库、不随安装包分发。

真实推理还需要上游代码与 CUDA 依赖（Python 3.10/3.11 推荐，本机 3.13 仅用于本项目的轻量测试）：

```powershell
git clone https://github.com/VAST-AI-Research/TripoSR
pip install -e TripoSR
# torch（与本机 CUDA 大版本一致）、transformers==4.35.0、rembg
pip install git+https://github.com/tatsy/torchmcubes.git
```

`torchmcubes` 在 Windows 需要 MSVC Build Tools + CUDA Toolkit 编译；官方 README 有专门的故障排查段。编译失败时上游网格提取会报错，本项目会把它作为 `INFERENCE_FAILED` 如实反馈，不会伪造成功。

## 显存与性能（官方口径，本机未实测）

| 项 | 值 | 依据 |
|---|---|---|
| 显存 | 约 6 GB（单图默认参数） | TripoSR README 官方声明 |
| 速度 | A100 上前向 < 0.5 s | 官方声明；消费卡未测 |
| 输入 | 统一 512×512（`cond_image_size`） | 上游 `config.yaml` |
| 网格 | marching cubes 默认 256，chunk 8192 | 上游 `run.py` 默认值 |

## 导出

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
| POST | `/api/jobs/{id}/cancel` | 请求取消（阶段边界生效） |
| GET | `/api/jobs/{id}/mesh` | 网格 JSON（positions / indices / colors / stats） |
| GET | `/api/jobs/{id}/export?format=obj\|glb` | 导出文件 |

任务状态：`QUEUED`、`RUNNING`、`NOT_READY`、`DONE`、`FAILED`、`TIMEOUT`、`CANCEL_REQUESTED`、`CANCELLED`。

## 测试

```powershell
pip install -r requirements-dev.txt
python -m pytest tests -q
```

覆盖输入校验、前景提示、模型探测、任务状态机（取消 / 超时 / 失败）、DEMO 分支、OBJ/GLB 结构与回读、API 端到端。测试不需要 GPU、不需要权重，也不联网。

## 许可

- 本项目外壳代码：**Apache-2.0**（见 `LICENSE`）。
- TripoSR 代码与权重：**MIT**；训练数据 Objaverse 精选子集 **CC-BY**。
- DINOv2 ViT-B/16（`facebook/dino-vitb16`）：**Apache-2.0**。
- rembg：**MIT**；u2net 权重来自 U-2-Net（**Apache-2.0**）。
- Three.js：**MIT**。
- 逐项说明见 [`docs/MODELS.md`](docs/MODELS.md)、[`NOTICE`](NOTICE)、[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。
- 本仓库**不包含**任何模型权重；权重许可不因本项目而改变。

## 已知限制

1. 真实推理未执行：没有权重、没有安装上游 `tsr` 与 torch 推理栈。
2. 本机 Python 3.13 与上游钉死的 `transformers==4.35.0`/旧版 torch 不兼容，真实推理需另建 3.10/3.11 环境。
3. `torchmcubes` 的 Windows 编译是最大不确定项；`rembg` 首跑需联网下载 u2net。
4. 取消/超时只在阶段边界生效，GPU 单次前向不可抢占。
5. 网格只驻留内存，重启服务即释放；大网格（mc 256）预览 JSON 体积较大，未做简化。
6. 前景提示只是颜色/alpha 统计，不等于 rembg 的抠图结果。
7. 生成质量受输入影响大：单主体、清晰、背景干净更稳；不承诺任意图片都能得到好模型。
