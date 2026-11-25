# TripoSR-Lite 发版档案（RELEASE_PROFILE）

- 版本单一事实源：`triposr_lite/settings.py` 的 `VERSION`（当前 `0.1.0`），与桌面壳三件套（`desktop/package.json`、`desktop/src-tauri/tauri.conf.json`、`desktop/src-tauri/Cargo.toml`）一致，由 `tests/test_desktop_version.py` 守护。
- 平台：Windows x64（Tauri v2 WebView2 + 当前用户 NSIS 安装器，无需管理员）。
- 产品名：`TripoSR-Lite`（安装显示名「TripoSR-Lite 单图重建工作台」）。

## 1. 端口与启动入口

| 项 | 契约 |
| --- | --- |
| 默认开发端口 | `127.0.0.1:7861`（`python -m triposr_lite serve` / `start-local.cmd`） |
| 桌面壳端口 | 动态回环：壳用 `TcpListener::bind("127.0.0.1:0")` 预抓空闲端口，再以 `--host 127.0.0.1 --port N` 传给启动脚本；禁止固定端口 |
| 启动入口 | `desktop` 壳 → `app/scripts/desktop_launch.py` → `uvicorn.run(triposr_lite.app:app)` |
| 端口日志锚点 | `desktop_launch.py` 首行输出 `监听端口: N`，落 `logs/desktop_python_*.log` |
| 浏览器 | 壳不调用 `webbrowser.open`；WebView 即界面。启动脚本不得打开浏览器 |

## 2. 健康与就绪

- 服务健康：`GET /api/health`，HTTP 200 且 JSON 可解析即「服务已启动」（与权重无关）。
- 模型就绪：`GET /api/model` 的 `probe.state` 为 `READY` / `NOT_READY`；`probe.reasons[]` 给出缺失项（`MISSING_CONFIG` / `MISSING_WEIGHTS` / `WEIGHT_TOO_SMALL` / `MISSING_TORCH` / `MISSING_UPSTREAM_CODE`）。
- 壳轮询 `/api/health`：500ms 间隔、单次 3s 超时、总超时 300s（`TRIPOSR_STARTUP_TIMEOUT_SECS` 仅测试用）；健康即导航主窗口到 `http://127.0.0.1:<port>/`。
- `/api/model` 的探测会在外部运行时里 `import torch`（首次冷启动实测约 50–170s，受冷缓存与杀软首扫影响），壳不阻塞导航，后台探测后写日志与状态；壳默认探测超时 300s、结果缓存 300s，避免把「慢」误报成 `RUNTIME_PROBE_FAILED`。
- 缺权重：服务照常启动；界面显示 `NOT_READY`、缺失清单与放置指南，预览区为 `DEMO` 演示网格，绝不伪造推理结果（生成按钮保持禁用）。

## 3. runtime 与模型边界

| 层 | 策略 |
| --- | --- |
| 应用运行时 | 打包 CPython 3.12.13（项目 `.venv` 的 base）+ `.venv\Lib\site-packages`，FastAPI/uvicorn/trimesh/Pillow 等 |
| 外部推理运行时 | 打包 CPython 3.12.10（`C:\Python312` 的 base）+ `external/TripoSR/.venv\Lib\site-packages`（torch 2.11.0+cu128、transformers、rembg、scikit-image，约 5.1GB）；**不重新下载 torch/Paddle 大 wheel** |
| 上游代码 | `external/TripoSR` 的跟踪代码（`run.py`、`tsr/`、`LICENSE`、`README.md`、`requirements.txt`）作为「指定 runtime」随包，位于 `app/external/TripoSR/`；不含 `examples/`、`figures/`、`.git/`、`.venv/` |
| 兼容层 | `scripts/_compat/torchmcubes`（CPU marching-cubes shim）随包保留，监督脚本通过 `PYTHONPATH` 注入 |
| 模型权重 | `models/triposr/model.ckpt`（1.68GB）与 `config.yaml` **不入库、不入包**；安装态模型目录 `TRIPOSR_MODEL_DIR=%LOCALAPPDATA%\TripoSR-Lite\models\triposr`（壳注入，可被外部环境变量覆盖） |
| 用户数据 | `TRIPOSR_RESULTS_DIR=%LOCALAPPDATA%\TripoSR-Lite\results`；壳日志 `%LOCALAPPDATA%\TripoSR-Lite\logs`；numba 编译缓存 `%LOCALAPPDATA%\TripoSR-Lite\cache\numba`（`NUMBA_CACHE_DIR`）；均不写安装目录 |
| 辅助下载 | `facebook/dino-vitb16` 与 rembg u2net 由上游首次推理时自动下载（HF 镜像默认 `https://hf-mirror.com`），包内不含 |

放置指南：`docs/MODELS.md`（官方下载地址、SHA256、镜像、许可）；安装后同一文档随包在 `app/docs/MODELS.md`，加载页「打开模型目录」直达 `%LOCALAPPDATA%\TripoSR-Lite\models\triposr`。

## 4. staging / 安装 / 卸载契约

### staging（`scripts/assemble_desktop_staging.ps1`）

```
<staging>/
├─ TripoSR-Lite.exe               # cargo build --release 产物（壳，不入数据卷）
└─ app/                           # 应用根（数据卷内容，staging 根 = 安装根）
   ├─ scripts/desktop_launch.py  run_triposr_real.py  _compat/
   ├─ triposr_lite/              # 代码 + web 静态页
   ├─ external/TripoSR/          # 上游代码 run.py tsr/ LICENSE ...
   │  └─ runtime/                # 外部推理运行时（第二套 CPython）
   ├─ runtime/                   # 应用运行时
   ├─ models/triposr/            # 空目录 + 放置说明，不含权重
   └─ docs/ README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ...
```

- 归档命令：`git archive HEAD` + Windows `System32\tar.exe` 解包；`settings.py` 必须随包（版本来源）。
- 上游代码与两套 runtime 属「指定 runtime」，从本机 checkout 与 `.venv` 复制，不经 `git archive`。
- 禁止归档：`models/` 权重、`results/`、`.venv/`、`external/` 的 `.git/`，`__pycache__`、`*.pyc`、日志、缓存。

### 安装（`scripts/installer/setup.nsi`）

- MUI2、`RequestExecutionLevel user`、`InstallDir "$LOCALAPPDATA\Programs\TripoSR-Lite"`，无需管理员。
- **安装器为引导式**：`TripoSR-Lite-Setup-v<version>.exe` 内嵌壳 exe 与独立 `7za.exe`；`TripoSR-Lite-Data.7z.001..NNN` 必须与 Setup 同目录（或 `data\` 子目录）。原因：NSIS 单文件安装包上限约 2GB，本包外部推理运行时约 5.1GB，无法内嵌；数据分卷仍是本产品的完整应用负载。
- `7za.exe` 不纳入源码仓库；构建时通过 `-SevenZipStandalone <path>` 显式指定仓外工具路径（只读内嵌进 Setup）。
- 解压：`"$EXEDIR\7za.exe" x "<data>\TripoSR-Lite-Data.7z.001" -o"$INSTDIR"`，失败即 Abort 并提示 SHA256 校验。
- 快捷方式：桌面 + 开始菜单；卸载信息写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\TripoSR-Lite`（DisplayName/DisplayVersion/UninstallString/EstimatedSize）。
- 安装时创建 `%LOCALAPPDATA%\TripoSR-Lite\{models\triposr,results,logs}`，覆盖安装不清理。
- 所有 MessageBox 均有 `IfSilent` 守卫：`/S` 静默安装不弹窗、不挂起。

### 卸载

- 静默 `/S`：默认**保留**用户数据（模型/结果/日志），只删程序目录与快捷方式/注册表。
- 交互卸载：询问「是否保留用户数据」，默认保留；选择删除才移除 `%LOCALAPPDATA%\TripoSR-Lite`。绝不静默删权重。

## 5. 产物

| 文件 | 说明 |
| --- | --- |
| `TripoSR-Lite-Setup-v<version>.exe` | NSIS 当前用户引导安装器（内嵌壳与 7za，约 3MB；数据卷另发） |
| `TripoSR-Lite-Data.7z.001..NNN` | 数据卷（`app/` 全量，单卷 ≤1900MB） |
| `SHA256SUMS` | 覆盖 Setup 与全部分卷（LF 行尾，支持 Git Bash `sha256sum -c`） |
| `upload-list.json` | 待上传清单（文件名/字节/SHA256） |

构建根：`D:/R5Build/TripoSR-Lite/`（`cargo-target`、`tmp`、`runtime`、`staging`、`dist`、`smoke`、`logs` 全在 D 盘；C 盘只落地最终 `%LOCALAPPDATA%` 安装态）。
