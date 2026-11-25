# 架构与边界

```
浏览器 (Three.js + OrbitControls)
   │  fetch JSON / multipart
   ▼
FastAPI (127.0.0.1:7861)
   ├── imaging.py     输入校验 + 粗略前景提示（非分割模型）
   ├── adapter.py     就绪探测（外部运行时子进程）+ 监督脚本子进程推理（应用进程不 import torch）
   ├── jobs.py        任务状态机（QUEUED/RUNNING/NOT_READY/DONE/FAILED/TIMEOUT/CANCELLED）
   ├── meshes.py      DEMO 网格、统计、OBJ/GLB 导出
   └── store.py       内存网格表（重启即释放）
```

## 推理链路

```
FastAPI → adapter.generate()
  → TRIPOSR_RUNTIME_PYTHON scripts/run_triposr_real.py
      → nvidia-smi 闸门（无其他 compute app + 空闲显存 ≥ TRIPOSR_MIN_FREE_MIB）
      → external/TripoSR/run.py（上游，未修改）
  → results/app-<时间戳>-<uuid>/mesh.glb → trimesh 读回 → InferenceResult
```

- 应用进程与项目 `.venv` 不安装也不导入 torch；探测在外部运行时里执行 `import torch` / `import tsr.system`（短超时、结果缓存 `TRIPOSR_PROBE_TTL_S`）。
- 每个任务独立目录，失败/取消/超时都保留 `request.json`、`run.log`、`supervisor.log`、`metrics.json` 等证据。
- 进度只跟随 `run.log` 的上游阶段行，不按时间伪造。

## 浏览器验收

`scripts/browser_real.py` 通过系统 Edge（Playwright）驱动页面：

- `--prepare-only`：只验证 NOT_READY 语义、预览、生成与导出按钮定位/禁用，不发 `POST /api/jobs`。
- 真实阶段：上传样本 → 点击 `#generateBtn` → 等待终态 → 下载 `#exportObjBtn` / `#exportGlbBtn`；`--format obj|glb|both`（默认 both）。
- OBJ 校验首行 `# TripoSR-Lite OBJ export`、`# source: model`、标签/对象名无 `DEMO`、`v`/`f` 非空且与页面统计一致；GLB 校验容器头、POSITION/索引计数与 trimesh 回读。
- 任务失败或下载失败时仍写 `acceptance.json`、`metrics.json`、`network.json`、`console.log`、`pageerror.log`，`flow` / `job` / `error` 记录失败原因。

## 状态机

- `POST /api/jobs` 先校验图片，再探测模型。
- 未就绪 → `NOT_READY`（终态）：不启动线程、不伪造推理，返回 `DEMO` 网格供预览。
- 就绪 → `QUEUED` → 后台线程 `RUNNING` → 阶段回调更新进度。
- `cancel`：排队阶段直接 `CANCELLED`；运行中置 `CANCEL_REQUESTED`，工作线程终止监督脚本子进程树后转 `CANCELLED`。
- 超时：轮询中检查 deadline，超时终止子进程树并转 `TIMEOUT`，结果不写入网格表。
- 上游异常 → `FAILED` + `error_code`（`NOT_READY` / `MISSING_*` / `RUNTIME_PROBE_FAILED` / `GPU_BUSY` / `INSUFFICIENT_VRAM` / `SUPERVISOR_FAILED` / `EMPTY_MESH`）。

> 取消/超时通过终止子进程实现，但受 0.2 s 轮询与进程退出等待影响；GPU 当前前向不会被信号抢占。

## 网格与导出

- 推理输出是上游 `trimesh.Trimesh`（顶点色）；直接驻留内存，不写用户磁盘。
- OBJ 由本项目自写：首行固定 `# TripoSR-Lite OBJ export`，`# source:` 只会是 `demo` 或 `model`，保留 `# vertices: N faces: M`，`v` 行可带 RGB 扩展，`f v//vn`。
- GLB 由 trimesh Scene 导出，节点名区分 `DEMO_NON_INFERENCE` 与 `TRIPOSR_MESH`。
- 导出响应带 `X-TripoSR-Demo` / `X-TripoSR-Source`，DEMO 文件名固定 `triposr_lite-demo.*`。

## 与 Scan-Memories 的边界

`Scan-Memories` 是 GLB/PLY 扫描浏览与回放工具（Vite + Three.js）。TripoSR-Lite 只负责「生成 + 导出」，不读取其目录、不写入其仓库、不共享运行时。导出的 GLB 可由用户自行拖入 Scan-Memories 浏览。

## 安全默认值

- 服务强制绑定回环地址；`python -m triposr_lite serve --host 0.0.0.0` 会被拒绝（退出码 2）。
- 上传文件只做内存解析，不落盘；不调用任何云服务。
- 权重目录与端口均可通过环境变量覆盖（`TRIPOSR_MODEL_DIR`、`TRIPOSR_TIMEOUT_S`），但默认不暴露到局域网。
