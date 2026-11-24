# 架构与边界

```
浏览器 (Three.js + OrbitControls)
   │  fetch JSON / multipart
   ▼
FastAPI (127.0.0.1:7861)
   ├── imaging.py     输入校验 + 粗略前景提示（非分割模型）
   ├── adapter.py     TripoSR 就绪探测 + 真实推理调用链（惰性导入 torch/tsr）
   ├── jobs.py        任务状态机（QUEUED/RUNNING/NOT_READY/DONE/FAILED/TIMEOUT/CANCELLED）
   ├── meshes.py      DEMO 网格、统计、OBJ/GLB 导出
   └── store.py       内存网格表（重启即释放）
```

## 状态机

- `POST /api/jobs` 先校验图片，再探测模型。
- 未就绪 → `NOT_READY`（终态）：不启动线程、不伪造推理，返回 `DEMO` 网格供预览。
- 就绪 → `QUEUED` → 后台线程 `RUNNING` → 阶段回调更新进度。
- `cancel`：排队阶段直接 `CANCELLED`；运行中置 `CANCEL_REQUESTED`，工作线程在阶段边界转 `CANCELLED`。
- 超时：工作线程在每个阶段边界检查 deadline，超时转 `TIMEOUT`。
- 上游异常 → `FAILED` + `error_code`（`NOT_READY` / `DEPENDENCY_MISSING` / `INFERENCE_FAILED` / `EMPTY_MESH`）。

> 取消与超时都不能打断 GPU 单次前向，只在阶段之间生效；这是刻意如实暴露的限制。

## 网格与导出

- 推理输出是上游 `trimesh.Trimesh`（顶点色）；直接驻留内存，不写用户磁盘。
- OBJ 由本项目自写：保留 source/label 注释，`v` 行可带 RGB 扩展，`f v//vn`。
- GLB 由 trimesh Scene 导出，节点名区分 `DEMO_NON_INFERENCE` 与 `TRIPOSR_MESH`。
- 导出响应带 `X-MeshMint-Demo` / `X-MeshMint-Source`，DEMO 文件名固定 `meshmint-demo.*`。

## 与 Scan-Memories 的边界

`Scan-Memories` 是 GLB/PLY 扫描浏览与回放工具（Vite + Three.js）。MeshMint-Lite 只负责「生成 + 导出」，不读取其目录、不写入其仓库、不共享运行时。导出的 GLB 可由用户自行拖入 Scan-Memories 浏览。

## 安全默认值

- 服务强制绑定回环地址；`python -m meshmint serve --host 0.0.0.0` 会被拒绝（退出码 2）。
- 上传文件只做内存解析，不落盘；不调用任何云服务。
- 权重目录与端口均可通过环境变量覆盖（`MESHMINT_MODEL_DIR`、`MESHMINT_TIMEOUT_S`），但默认不暴露到局域网。
