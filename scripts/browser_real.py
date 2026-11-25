"""TripoSR-Lite 浏览器验收脚本。

阶段 A（--prepare-only）只验证 NOT_READY 页面语义、文件预览与按钮禁用，绝不触发推理；
真实阶段通过页面控件上传样本、启动任务、预览网格并下载 GLB，再做结构校验。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

PROJECT = "TripoSR-Lite"
DEFAULT_PORT = 8876
VIEWPORT = {"width": 1440, "height": 1000}
BASELINE = {"vertices": 42076, "faces": 84156}
REPO_ROOT = Path(__file__).resolve().parents[1]


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="TripoSR-Lite 浏览器验收（--prepare-only 只验证 NOT_READY 语义）"
    )
    parser.add_argument("--base-url", default=f"http://127.0.0.1:{DEFAULT_PORT}")
    parser.add_argument("--input", default="external/TripoSR/examples/chair.png")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--timeout", type=float, default=900.0, help="秒；默认 900")
    parser.add_argument("--service-log", default=None, help="可选：服务端日志路径，写入 acceptance")
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="只验证页面状态、文件预览与按钮禁用语义，禁止触发真实任务",
    )
    return parser.parse_args(argv)


def write_json(path: Path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_screenshot(path: Path, viewport: dict) -> dict:
    from PIL import Image, ImageStat

    image = Image.open(path).convert("RGB")
    stat = ImageStat.Stat(image)
    stddev = sum(stat.stddev) / 3.0
    ok = image.size == (viewport["width"], viewport["height"]) and stddev > 3.0
    return {"file": path.name, "size": list(image.size), "stddev": round(stddev, 2), "ok": ok}


def glb_structure(path: Path) -> dict:
    """Structural parse of a GLB container: header, JSON chunk, mesh/primitive counts."""
    data = path.read_bytes()
    result = {"bytes": len(data), "magic_ok": False, "version": None, "declared_length": None}
    if len(data) < 20:
        return result
    magic, version, declared = struct.unpack_from("<4sII", data, 0)
    result.update(
        {
            "magic_ok": magic == b"glTF",
            "version": version,
            "declared_length": declared,
            "length_matches": declared == len(data),
        }
    )
    chunk_len, chunk_type = struct.unpack_from("<I4s", data, 12)
    if chunk_type != b"JSON":
        return result
    try:
        document = json.loads(data[20 : 20 + chunk_len].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return result

    accessors = document.get("accessors") or []
    meshes = document.get("meshes") or []
    primitive_count = 0
    position_count = 0
    index_count = 0
    for mesh in meshes:
        for primitive in mesh.get("primitives") or []:
            primitive_count += 1
            attributes = primitive.get("attributes") or {}
            accessor = attributes.get("POSITION")
            if isinstance(accessor, int) and accessor < len(accessors):
                position_count += int(accessors[accessor].get("count") or 0)
            indices = primitive.get("indices")
            if isinstance(indices, int) and indices < len(accessors):
                index_count += int(accessors[indices].get("count") or 0)
    result.update(
        {
            "meshes": len(meshes),
            "primitives": primitive_count,
            "positions": position_count,
            "indices": index_count,
            "materials": len(document.get("materials") or []),
        }
    )
    return result


MESH_PROBE = """
import json, sys
import trimesh
path = sys.argv[1]
mesh = trimesh.load(path, force="mesh")
print(json.dumps({
    "vertices": int(len(mesh.vertices)),
    "faces": int(len(mesh.faces)),
    "watertight": bool(mesh.is_watertight),
}))
"""


def mesh_stats(path: Path) -> dict:
    """trimesh read-back; falls back to the repo interpreter when trimesh is absent."""
    try:
        import trimesh  # noqa: F401
    except Exception:  # noqa: BLE001 - the acceptance venv may not carry trimesh
        python = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
        if not python.is_file():
            return {"error": "trimesh 不可用且找不到项目解释器"}
        try:
            proc = subprocess.run(
                [str(python), "-c", MESH_PROBE, str(path)],
                capture_output=True, text=True, encoding="utf-8", timeout=180,
            )
            if proc.returncode != 0:
                return {"error": (proc.stderr or "").strip()[-400:]}
            return json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception as exc:  # noqa: BLE001
            return {"error": f"{type(exc).__name__}: {exc}"}

    try:
        import trimesh

        mesh = trimesh.load(str(path), force="mesh")
        return {
            "vertices": int(len(mesh.vertices)),
            "faces": int(len(mesh.faces)),
            "watertight": bool(mesh.is_watertight),
        }
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}"}


def parse_mesh_stats(text: str) -> dict:
    numbers = [int(item.replace(",", "")) for item in re.findall(r"[\d,]+", text or "")]
    stats = {}
    if len(numbers) >= 2:
        stats["vertices"], stats["faces"] = numbers[0], numbers[1]
    stats["watertight"] = "watertight yes" in (text or "").lower()
    return stats


def main(argv=None) -> int:
    args = parse_args(argv)
    base = args.base_url.rstrip("/")
    port = urlparse(base).port

    input_path = Path(args.input).expanduser()
    if not input_path.is_absolute():
        input_path = (REPO_ROOT / input_path).resolve()
    if not input_path.is_file():
        print(f"[blocked] 样本不存在：{input_path}", file=sys.stderr)
        return 2

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    prefix = "browser-prep" if args.prepare_only else "browser-real"
    out_dir = Path(args.output_dir) if args.output_dir else REPO_ROOT / "results" / f"{prefix}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:  # noqa: BLE001
        print(f"[blocked] Playwright 不可用：{exc}", file=sys.stderr)
        return 2

    started = now_iso()
    t0 = time.time()
    checks = {
        "status_ready": False,
        "status_not_ready": False,
        "model_state_notready": False,
        "notready_pill": False,
        "notready_reasons": False,
        "generate_disabled_before_upload": False,
        "generate_disabled_after_upload": False,
        "demo_placeholder": False,
        "viewer_canvas": False,
        "file_preview_visible": False,
        "no_job_created": False,
        "export_glb_disabled": False,
        "no_third_party_requests": False,
        "screenshots_valid": False,
        "layout_ok": False,
        "real_job_started": False,
        "terminal_success": False,
        "job_demo_false": False,
        "result_visible": False,
        "no_demo": False,
        "download_nonempty": False,
        "export_headers_ok": False,
        "artifact_valid": False,
    }
    console_lines: list[str] = []
    page_errors: list[str] = []
    network: list[dict] = []
    export_headers: dict = {}
    job_response: dict = {}
    screenshots: list[dict] = []
    artifacts: dict = {}
    status_payload: dict = {}
    probe_state = ""
    page_mesh_stats: dict = {}
    layout: dict = {}
    error_msg = ""
    notes: list[str] = []

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(channel="msedge", headless=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[blocked] 无法启动系统 Edge：{exc}", file=sys.stderr)
            return 2

        ctx = browser.new_context(viewport=VIEWPORT, accept_downloads=True, locale="zh-CN")
        page = ctx.new_page()
        page.on("console", lambda m: console_lines.append(f"[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: page_errors.append(str(e)))

        def on_response(r) -> None:
            entry = {
                "method": r.request.method,
                "url": r.url,
                "status": r.status,
                "resource_type": r.request.resource_type,
                "headers": dict(r.headers),
            }
            network.append(entry)
            if "/export" in r.url and "format=glb" in r.url:
                export_headers.update({k: v for k, v in r.headers.items()})
            if r.request.method == "POST" and r.url.split("?")[0].endswith("/api/jobs"):
                try:
                    job_response.update(r.json())
                except Exception:  # noqa: BLE001 - body may not be JSON
                    pass

        page.on("response", on_response)

        def same_origin(url: str) -> bool:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return True
            return parsed.hostname in ("127.0.0.1", "localhost", "::1") and parsed.port == port

        def shot(name: str) -> None:
            path = out_dir / name
            page.screenshot(path=str(path), full_page=False)
            screenshots.append(validate_screenshot(path, VIEWPORT))

        try:
            status_payload = ctx.request.get(f"{base}/api/model", timeout=60_000).json()
        except Exception as exc:  # noqa: BLE001
            print(f"[blocked] 状态端点 /api/model 不可用：{exc}", file=sys.stderr)
            browser.close()
            return 2

        probe = status_payload.get("probe") or {}
        probe_state = str(probe.get("state") or "")
        ready = probe_state == "READY"
        checks["status_ready"] = ready
        checks["status_not_ready"] = not ready

        if args.prepare_only and ready:
            error_msg = "准备阶段要求 NOT_READY 开发模式，但 /api/model probe.state=READY"
        if not args.prepare_only and not ready:
            print(f"[blocked] 真实阶段要求 READY，当前 {probe_state}", file=sys.stderr)
            browser.close()
            return 2

        page.goto(base + "/", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_function(
            "document.getElementById('modelState').textContent !== '检测中…'",
            timeout=60_000,
        )
        page.wait_for_function(
            "(() => { const c = document.querySelector('#viewport canvas');"
            " return c && c.width > 0 && c.height > 0; })()",
            timeout=60_000,
        )
        checks["viewer_canvas"] = True

        layout = page.evaluate(
            "(() => { const m = document.querySelector('main'); const r = m ? m.getBoundingClientRect() : null;"
            " return { title: document.title, scrollWidth: document.documentElement.scrollWidth,"
            " innerWidth: window.innerWidth, mainWidth: r ? r.width : 0,"
            " mainHeight: r ? r.height : 0, mainRight: r ? r.right : 0 }; })()"
        )
        model_state = page.locator("#modelState").inner_text().strip()
        pill_text = page.locator("#statusPill").inner_text().strip()
        reasons_text = page.locator("#modelReasons").inner_text()
        mesh_meta = page.locator("#meshMeta").inner_text().strip()
        demo_badge_visible = page.locator("#demoBadge").is_visible()
        demo_badge_text = page.locator("#demoBadge").inner_text().strip()

        if args.prepare_only:
            checks["model_state_notready"] = model_state == "NOT_READY"
            checks["notready_pill"] = "模型未就绪" in pill_text
            checks["notready_reasons"] = bool(reasons_text.strip()) and (
                "MISSING_WEIGHTS" in reasons_text or "MISSING_CONFIG" in reasons_text
            )
            checks["generate_disabled_before_upload"] = page.locator("#generateBtn").is_disabled()
            checks["demo_placeholder"] = (
                demo_badge_visible
                and "DEMO" in demo_badge_text
                and "DEMO placeholder" in mesh_meta
            )
            shot("01-notready.png")
        else:
            checks["model_state_notready"] = True  # READY 模式；NOT_READY 已在状态端点拦截
            if not pill_text:
                error_msg = "页头状态徽标为空"
            shot("01-ready.png")

        page.set_input_files("#fileInput", str(input_path))
        page.wait_for_function(
            "(() => { const img = document.getElementById('originalPreview');"
            " return document.getElementById('imageRow').hidden === false && img.src.length > 0; })()",
            timeout=60_000,
        )
        page.wait_for_function(
            "document.getElementById('fileHint').textContent.indexOf('KB') !== -1",
            timeout=60_000,
        )
        file_hint = page.locator("#fileHint").inner_text().strip()
        checks["file_preview_visible"] = "PNG" in file_hint or "JPEG" in file_hint

        if args.prepare_only:
            checks["generate_disabled_after_upload"] = page.locator("#generateBtn").is_disabled()
            checks["export_glb_disabled"] = page.locator("#exportGlbBtn").is_disabled()
            shot("02-preview-notready.png")
            posts = [
                n for n in network
                if n["method"] == "POST" and n["url"].split("?")[0].endswith("/api/jobs")
            ]
            checks["no_job_created"] = not posts and not job_response
        else:
            page.click("#generateBtn")
            checks["real_job_started"] = True
            try:
                page.wait_for_function(
                    "(() => { const t = document.getElementById('statusPill').textContent || '';"
                    " return t.indexOf('推理中') !== -1 || t.indexOf('加载') !== -1; })()",
                    timeout=60_000,
                )
                shot("02-progress.png")
            except Exception:  # noqa: BLE001 - job may finish faster than the sampler
                notes.append("进度截图未捕获到推理中状态，改用任务完成后截图")
            page.wait_for_function(
                "(() => { const t = document.getElementById('statusPill').textContent || '';"
                " return t.indexOf('完成') !== -1 || t.indexOf('失败') !== -1"
                " || t.indexOf('超时') !== -1 || t.indexOf('已取消') !== -1; })()",
                timeout=int(args.timeout * 1000),
            )
            final_pill = page.locator("#statusPill").inner_text().strip()
            job = job_response.get("job") or {}
            checks["terminal_success"] = "完成" in final_pill
            checks["job_demo_false"] = job.get("demo") is False
            if not checks["terminal_success"]:
                error_msg = f"任务未达到成功终态：{final_pill}"
            mesh_stats_text = page.locator("#meshStats").inner_text().strip()
            page_mesh_stats = parse_mesh_stats(mesh_stats_text)
            checks["result_visible"] = bool(mesh_stats_text) and page.locator("#demoBadge").is_visible() is False
            checks["no_demo"] = (
                not page.locator("#demoBadge").is_visible()
                and "DEMO" not in page.locator("#meshMeta").inner_text()
            )
            shot("03-result.png")

            target = None
            try:
                if page.locator("#exportGlbBtn").is_disabled():
                    raise RuntimeError("任务终态后导出 GLB 按钮仍为禁用")
                with page.expect_download(timeout=120_000) as download_info:
                    page.click("#exportGlbBtn")
                download = download_info.value
                target = out_dir / (download.suggested_filename or "triposr_lite-mesh.glb")
                download.save_as(str(target))
                checks["download_nonempty"] = target.stat().st_size > 0
            except Exception as exc:  # noqa: BLE001 - the acceptance file must survive a broken download leg
                error_msg = error_msg or f"GLB 下载失败：{type(exc).__name__}: {exc}"

            if target is not None:
                size = target.stat().st_size
                structure = glb_structure(target)
                stats = mesh_stats(target)
                baseline_match = (
                    stats.get("vertices") == BASELINE["vertices"]
                    and stats.get("faces") == BASELINE["faces"]
                )
                artifacts = {
                    "file": target.name,
                    "bytes": size,
                    "sha256": sha256_file(target),
                    "glb": structure,
                    "mesh": stats,
                    "page_mesh_stats": page_mesh_stats,
                    "baseline": BASELINE,
                    "baseline_match": baseline_match,
                    "headers": {
                        "X-TripoSR-Demo": export_headers.get("x-triposr-demo"),
                        "X-TripoSR-Source": export_headers.get("x-triposr-source"),
                    },
                }
                demo_header = artifacts["headers"]["X-TripoSR-Demo"]
                source_header = artifacts["headers"]["X-TripoSR-Source"]
                checks["export_headers_ok"] = str(demo_header).lower() != "true" and source_header == "model"
                checks["artifact_valid"] = bool(
                    structure.get("magic_ok")
                    and structure.get("positions")
                    and structure.get("indices")
                    and stats.get("vertices")
                    and stats.get("faces")
                    and stats.get("watertight")
                )
                if stats.get("error"):
                    error_msg = error_msg or f"GLB 回读失败：{stats['error']}"
                elif not stats.get("watertight"):
                    error_msg = error_msg or "GLB 回读网格非 watertight"
                if not baseline_match:
                    notes.append(
                        f"顶点/面与基线不一致：{stats.get('vertices')}/{stats.get('faces')}"
                        f" vs {BASELINE['vertices']}/{BASELINE['faces']}"
                    )
                shot("04-downloaded.png")

        third_party = [n for n in network if not same_origin(n["url"])]
        checks["no_third_party_requests"] = not third_party
        checks["screenshots_valid"] = all(s["ok"] for s in screenshots) and bool(screenshots)
        checks["layout_ok"] = (
            layout["scrollWidth"] <= layout["innerWidth"] + 1
            and layout["mainWidth"] > 0
            and layout["mainHeight"] > 0
        )
        if not checks["layout_ok"]:
            error_msg = error_msg or "页面存在横向溢出或主体不可见，截图可能被裁切"

        ctx.close()
        browser.close()

    required = (
        [
            "status_not_ready",
            "model_state_notready",
            "notready_pill",
            "notready_reasons",
            "generate_disabled_before_upload",
            "generate_disabled_after_upload",
            "demo_placeholder",
            "viewer_canvas",
            "file_preview_visible",
            "no_job_created",
            "export_glb_disabled",
        ]
        if args.prepare_only
        else [
            "status_ready",
            "no_demo",
            "file_preview_visible",
            "real_job_started",
            "terminal_success",
            "job_demo_false",
            "result_visible",
            "download_nonempty",
            "export_headers_ok",
            "artifact_valid",
        ]
    )
    ok = (
        not error_msg
        and all(checks[key] for key in required)
        and checks["screenshots_valid"]
        and checks["no_third_party_requests"]
        and checks["layout_ok"]
    )

    finished = now_iso()
    acceptance = {
        "project": PROJECT,
        "base_url": base,
        "started_at": started,
        "finished_at": finished,
        "prepare_only": bool(args.prepare_only),
        "browser": {"channel": "msedge", "headless": True, "viewport": VIEWPORT},
        "input": {
            "path": str(input_path),
            "bytes": input_path.stat().st_size,
            "sha256": sha256_file(input_path),
        },
        "checks": checks,
        "required": required,
        "artifacts": artifacts,
        "screenshots": screenshots,
        "service_log": args.service_log or "见服务端启动输出（uvicorn stdout）",
        "notes": notes,
        "ok": ok,
        "error": error_msg,
    }
    metrics = {
        "project": PROJECT,
        "mode": "prepare-only" if args.prepare_only else "real",
        "base_url": base,
        "port": port,
        "duration_s": round(time.time() - t0, 2),
        "probe_state": probe_state,
        "probe_reasons": probe.get("reasons"),
        "model_state": model_state,
        "status_pill": pill_text,
        "mesh_meta": mesh_meta,
        "page_mesh_stats": page_mesh_stats,
        "layout": layout,
        "requests": len(network),
        "third_party_requests": len(third_party),
        "job": job_response.get("job") or None,
        "exit_code": 0 if ok else 1,
    }
    write_json(out_dir / "acceptance.json", acceptance)
    write_json(out_dir / "metrics.json", metrics)
    write_json(out_dir / "network.json", network)
    (out_dir / "console.log").write_text("\n".join(console_lines), encoding="utf-8")
    (out_dir / "pageerror.log").write_text("\n".join(page_errors), encoding="utf-8")

    print(f"[{PROJECT}] {'OK' if ok else 'FAIL'} -> {out_dir}")
    for key, value in checks.items():
        print(f"  {key}: {value}")
    for note in notes:
        print(f"  note: {note}")
    if error_msg:
        print(f"  error: {error_msg}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
