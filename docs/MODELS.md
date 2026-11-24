# 模型清单与许可（TripoSR-Lite）

最后核查：2026-11-24（只读核对官方仓库与模型卡，未下载权重）。

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
| 本机验证 | **未执行：未下载权重** |

### 下载

```powershell
huggingface-cli download stabilityai/TripoSR --local-dir models/triposr --include config.yaml model.ckpt
```

校验建议：`model.ckpt` 应约为 1.68 GB；明显偏小说明下载中断。服务会把小于 1 GB 的文件判为 `WEIGHT_TOO_SMALL`（阈值可用 `TRIPOSR_MIN_CKPT_BYTES` 调整）。

### 上游代码安装（真实推理必需）

```powershell
git clone https://github.com/VAST-AI-Research/TripoSR
pip install -e TripoSR
pip install git+https://github.com/tatsy/torchmcubes.git
```

依赖：Python ≥ 3.8（Windows 建议 3.10/3.11）、与本机 CUDA 大版本一致的 PyTorch、`transformers==4.35.0`、`rembg`、`trimesh`、`einops`、`omegaconf`。纹理烘焙额外需要 `xatlas`、`moderngl`，首版不使用。

## 2. 附带权重（自动下载，不随本仓库分发）

| 组件 | 用途 | 许可 | 体积 | 触发时机 |
|---|---|---|---|---|
| `facebook/dino-vitb16`（DINOv2 ViT-B/16） | TripoSR 图像 tokenizer | **Apache-2.0** | 约 0.33 GB（估算） | 首次 `TSR.from_pretrained` 时由 transformers 下载 |
| `rembg` / u2net | 推理前抠图 | rembg **MIT**；u2net 权重来自 U-2-Net，**Apache-2.0** | 约 176 MB | 首次真实推理且未跳过抠图 |

两者都只在真实推理路径上出现；DEMO 与测试路径不下载。

## 3. 许可边界

- 本项目外壳代码：**Apache-2.0**。
- 上游 TripoSR 代码与权重仍是 **MIT**，不因外壳而改变；本项目不复制其源码。
- Objaverse 的 CC-BY 署名保留在 `NOTICE`。
- 模型卡的使用限制（不得生成令人不适、传播刻板印象的内容）由使用者自行遵守。

## 4. 未执行项

- 真实 TripoSR 推理：**未执行：未下载权重**，也未安装 torch / torchmcubes / rembg。
- 显存与耗时：只有官方声明，没有本机（RTX 5070 Ti Laptop 12GB）实测数据。
- GLB 贴图烘焙：未实现，只导出顶点色网格。
