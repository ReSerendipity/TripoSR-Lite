# ADR：CLI 适配与更新路线收敛

- 日期：2026-11-26
- 状态：Accepted（本轮执行依据）
- 范围：仅 `C:\Users\Doro\APP\repository\TripoSR-Lite`

## 1. 背景与问题

TripoSR-Lite 目前是「能跑的 Web 服务 + Tauri 桌面壳」。已有 `python -m triposr_lite serve` / `probe` 两个 argparse 命令，`probe` 输出 JSON，是六项目改造中唯一可复用的 CLI 起点。但它不是产品 CLI：

- 退出码只有 0/2（2 = 拒绝非回环 host），无 1/3 语义；
- `probe` 的 JSON 是裸 payload，没有 `ok/data/error` 信封；
- 全功能（inspect/generate/jobs/mesh/export 等）只能走 HTTP；
- 安装器不注册全局命令，AI agent 无法用一条命令完成任务；
- 桌面壳无检查更新 / updater，也没有自更新重启闭环。

## 2. 现状复核（动手前亲自读源码 + 命令实测）

| 维度 | 复核结论 | 证据 |
|---|---|---|
| 技术栈 | Python 3.12.13，无 pyproject/setup.py；FastAPI/uvicorn/trimesh/Pillow | `.venv/Scripts/python.exe -c "import sys;print(sys.version)"`、`requirements.txt` |
| 现有 CLI | `triposr_lite/__main__.py` 75 行，`serve` / `probe`；`probe` 裸 JSON；退出码 0/2 | 源码逐行读 |
| API 端点 | 9 个 `/api/*`：`health`、`model`、`demo`、`inspect`、`jobs`（POST/GET）、`jobs/{id}/cancel`、`jobs/{id}/mesh`、`jobs/{id}/export`，另有 `/` 静态页；无 `/v1` | `triposr_lite/app.py` 逐行读 |
| 引擎 | 单一 TripoSR；`models/triposr/model.ckpt` 实测 1,677,246,742 B、`config.yaml` 987 B | `ls -la models/triposr/` |
| 模型加载 | 每任务由 `adapter.generate()` 起外部运行时子进程执行 `scripts/run_triposr_real.py`，**不常驻**；应用进程不 import torch | `adapter.py` 第 408–675 行 |
| 桌面壳 | Tauri v2；命令仅 `get_app_info`/`get_startup_status`/`retry_start`/`open_model_dir`；**无任何 updater / check-update 代码** | `main.rs` 全文 + 全 `desktop/` 搜索 updater/update 零命中 |
| 端口 | `port_manager.rs` 预抓空闲端口 → `desktop_launch.py` 打印 `监听端口: N`；无端口文件 | 两文件逐行读 |
| 安装器 | 引导式 7z 分卷 NSIS（当前用户），无 shim、无 PATH、无 CLI 快捷方式，`/S` 仅静默安装/卸载 | `scripts/installer/setup.nsi` 全文 |
| 鉴权/CSRF | 应用内无任何 middleware；`/api/*` 无握手、无 CSRF、无鉴权 | `app.py` 无 middleware 注册 |
| 测试基线 | **86 passed**（任务书写 75，差异）；`requirements-dev.txt` 只有 pytest/httpx | `.venv/Scripts/python.exe -m pytest -q` 退出码 0 |
| 环境 | `makensis` 与 `cargo` 可用；ruff/mypy 未装（随 S2 补） | `command -v` / `python -m ruff --version` |
| 已知阻塞 | torchmcubes CUDA 编译失败 → `scripts/_compat/torchmcubes` CPU shim（scikit-image lewiner），DINO/NeRF 前向仍在 CUDA；transformers 版本需在外部运行时实测确认 | shim 源码 + `docs/RELEASE_PROFILE.md` |

### 与任务书第 2 节的差异清单

1. 测试数：任务书 75 函数，实测 **86 passed**。
2. 必读材料 10 的路径 `TTS-MultiModel/app/integrated_app/vendor/breeze/*` 不存在；实际为 `app/integrated_app/engines/breeze_tts2/vendor/*` 与 `app/integrated_app/breeze_runtime.py`，已按实际路径阅读。
3. 更新路线：任务书称「无检查更新 / updater；`/S` 仅用于静默卸载」，复核确认**连死配置都没有**（无 `tauri_plugin_updater`、无 `shell-update.json`）。因此 U 阶段不是「删掉重复路线」，而是**从零建立单一完整包路线**。
4. 安装器数据卷：任务书写 1.68GB 权重，实际随包的是约 5.1GB 外部推理 runtime，权重不入包（1.68GB 权重由用户自己放）。NSIS Setup 只是引导器 + 7z 分卷。
5. `RELEASE_PROFILE.md` 已有完整安装/卸载契约与版本单一事实源（`settings.py` VERSION = 桌面三件套 = 0.1.0），S1/U 必须保持不破坏。

## 3. 功能盘点与产品 CLI 边界

| 来源 | 可暴露能力 | CLI 落点 |
|---|---|---|
| `probe` / `/api/model` / `/api/health` | 就绪、失败原因、版本 | `probe`、`model status`、`system health/gpu/config` |
| `/api/inspect` | 图片校验 + 前景占比提示 | `inspect` |
| `POST /api/jobs` + `adapter.generate` | 单图重建 | `generate`、`batch generate/resume` |
| `/api/jobs/{id}`、`.../cancel` | 任务查询与取消 | `jobs list/show/cancel` |
| `/api/jobs/{id}/mesh`、`.../export` | 网格统计与 OBJ/GLB 导出 | `mesh show/export`、`export` |
| `registry.MODEL_CARD` | 权重清单、下载地址、许可、GPU 要求 | `models list/show`、`engines list`、`model download/verify` |
| `serve` | 本地 Web 服务 | `serve`（保留并强化，仍拒绝非回环 host） |

**选型**：单包多命令族（`argparse` 子命令）+ Result 信封 + Human/JSON 双输出 + 默认直调 service layer、可选 HTTP 复用常驻服务。不引入 click/typer（安装态 runtime 不额外装依赖）。

## 4. 统一输出与退出码契约

信封固定为 `{"ok": bool, "command": str, "data": {...}}` 或 `{"ok": false, "command": str, "error": str, "error_code": str}`；`warnings` 可选。`--json` 在 `main()` 里做 argv 预提取，任意位置生效；JSON 模式下 stdout 只承载信封，其他 print 重定向到 stderr。

| 退出码 | 语义 | 触发 error_code（示例） |
|---|---|---|
| 0 | 成功 | — |
| 1 | 运行时/业务错误 | `BUSINESS_ERROR`、`SUPERVISOR_FAILED`、`EMPTY_MESH`、`GLB_EXPORT_FAILED`、`TIMEOUT`（CLI 侧硬超时）、`REMOTE_FAILED` 后回落也失败 |
| 2 | 用法错误 | `USAGE_ERROR`、`INVALID_FORMAT`、`JOB_NOT_FOUND`（调用方给错 id） |
| 3 | 未就绪（NOT_READY 类） | `NOT_READY`、`MISSING_CONFIG`、`MISSING_WEIGHTS`、`WEIGHT_TOO_SMALL`、`MISSING_TORCH`、`MISSING_UPSTREAM_CODE`、`RUNTIME_PROBE_FAILED`、`GPU_BUSY`、`INSUFFICIENT_VRAM` |

`GPU_BUSY` / `INSUFFICIENT_VRAM` 归入 3 并附修复指引（检查占用进程 / `TRIPOSR_MIN_FREE_MIB`），因为它们是「环境未满足前置条件」而非代码缺陷；`EMPTY_MESH` 归入 1（已执行但产出不可用）。映射表由 `cli_output.py` 单点实现，测试钉住。

## 5. 分阶段决策

### S1 · 全局命令

- 命令名选 **`triposr`**（短、与桌面产品名 `triposr-lite` 区分；`triposr-lite` 保留为 shim 内的显示名）。写入本节即定案，不再另做选择题。
- shim 路径：`%INSTDIR%\triposr.cmd` → `%INSTDIR%\app\runtime\python.exe` → `%INSTDIR%\app\app\integrated_app\cli.py`。安装态目录层级来自 `RELEASE_PROFILE.md` 的 staging 契约（`git archive` 解到 `app/`）。
- NSIS 用 `StrFunc.nsh` 做 PATH 幂等追加/精确移除；安装段与卸载段分别 `${Using:StrFunc}`；广播 `WM_SETTINGCHANGE`；开始菜单 CLI 快捷方式用 `$SYSDIR\cmd.exe`。保持 7z 分卷引导结构不动。
- 不改桌面壳端口逻辑。

### S2 · P0→P4

- 源码落在仓根 `app/integrated_app/`（安装后即 `<inst>\app\app\integrated_app\`，与 shim 对齐）。脚本直跑时 `sys.path` 注入仓根 + 绝对导入。
- 同步把旧 `triposr_lite/__main__.py` 的 `probe` / `serve` 委托到新实现，保证 `python -m triposr_lite probe --json` 也返回 Result 信封（原裸 JSON 行为改变属预期，README 同步）。
- `generate` 产物固定落 `results/cli/`（可用 `TRIPOSR_CLI_RESULTS_DIR` 覆盖），文件名含 job id，避免相互覆盖。
- 随 S2 补最小 ruff / mypy / pre-commit / CI 配置（pytest + ruff + cargo gate）。

### S3 · 选择方案 B（保留子进程加载，如实说明收益边界）

决策依据：模型前向必须跑在外部运行时的 torch 里（`app` 运行时刻意不装 torch），当前 `run_triposr_real.py` 已通过 GPU 闸门 + 全量证据目录的真实推理路径验证。把它改成常驻 worker 需要重写监督协议、上游 `run.py` 调用方式与取消/超时语义，风险与工作量超出本轮 S3 1.5–2.5 人·日的预算，且可能破坏已验收的真实链路。

因此选择 **方案 B**：CLI 的 HTTP 复用**只省 FastAPI 启动与进程初始化，不省 1.68GB 权重加载**。文档与 KPI 实测如实说明，绝不宣称「几十秒降到个位数秒」。若后续要上方案 A，应另立 ADR 并先做常驻 worker 的独立验收。

S3 其余实现：新增端口文件（写侧在 `desktop_launch.py`，读侧在 `cli_remote.py`），服务发现优先级 `--server` > `TRIPOSR_CLI_SERVER` > 端口文件；2s 探测；`inspect`/`generate`/`batch` 可走 HTTP，`--no-remote` 强制直调；任何异常回落直调；大网格走流式下载。

### S4 · 引擎阻塞

优先级：先查分派/注册、再做「加载前 0 秒校验」（缺权重 / 显存不足 / GPU 忙必须在起子进程前给出语义错误），然后核实 torchmcubes CPU shim 与 transformers 版本；主环境锁不动。质量类问题先人眼/下游可用性定性，数值指标只做回归断言。

### U · 更新路线

复核后确认是「零路线」而非「两条路线」。目标仍是**单一完整包路线**：官网与客户端检查更新给同一个 `TripoSR-Lite-Setup-v<version>.exe` + 同名 `.sha256`；本仓完整负载是 Setup + 7z 分卷组合，前端必须提示分卷与 Setup 同目录（不引入差分/增量双路线）。自更新七步中第 7 步由自写 NSIS 的 `/RELAUNCH` 参数完成。Release 资产默认不动。

## 6. 验收纪律

所有结论以命令输出、退出码、`json.loads`、pytest 数、墙钟时间为准；每阶段独立提交（本轮只 commit 不 push），提交前 pytest + ruff + 改动文件 mypy 净增 0 + `cargo test` / `cargo clippy -D warnings`。清理试探产物一律精确单文件路径。
