"""U 阶段静态契约：Rust 传参 ↔ NSIS 检测 ↔ 前端监听 ↔ CI 端到端 三/四处成对存在。"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAURI = ROOT / "desktop" / "src-tauri"
NSI = ROOT / "scripts" / "installer" / "setup.nsi"
INDEX = ROOT / "desktop" / "src" / "index.html"
WORKFLOW = ROOT / ".github" / "workflows" / "verify-relaunch.yml"


def test_rust_side_emits_relaunch_flag() -> None:
    updater = (TAURI / "src" / "updater.rs").read_text(encoding="utf-8")
    main = (TAURI / "src" / "main.rs").read_text(encoding="utf-8")
    build = (TAURI / "build.rs").read_text(encoding="utf-8")
    cargo = (TAURI / "Cargo.toml").read_text(encoding="utf-8")
    assert 'fn installer_args() -> Vec<&\'static str>' in updater
    assert 'vec!["/S", "/RELAUNCH"]' in updater
    assert 'app.emit(\n        "update-installing"' in updater
    assert "pub fn check_update()" in updater
    assert "pub fn install_update(" in updater
    assert "TripoSR-Lite-Setup-v" in updater and "SHA256SUMS" in updater
    # 生产代码不再回退增量 zip；测试夹具里保留一个 zip 用来验证它会被忽略。
    assert ".zip" not in updater.split("#[cfg(test)]")[0]
    assert "mod updater;" in main
    assert "check_update," in main and "install_update," in main
    assert "check_update" in build and "install_update" in build
    assert "reqwest" in cargo and "sha2" in cargo and "hex" in cargo


def test_capabilities_allow_update_commands() -> None:
    import json

    caps = json.loads((TAURI / "capabilities" / "main.json").read_text(encoding="utf-8"))
    assert "allow-check-update" in caps["permissions"]
    assert "allow-install-update" in caps["permissions"]


def test_nsis_side_detects_relaunch_flag() -> None:
    text = NSI.read_text(encoding="utf-8-sig")
    assert '${StrStr} $0 "$CMDLINE" "/RELAUNCH"' in text
    assert 'Exec \'"$INSTDIR\\${APP_EXE}"\'' in text
    assert 'WriteUninstaller "$INSTDIR\\uninstall.exe"' in text
    # S1 的 StrFunc 前置必须仍在，否则 /RELAUNCH 段无法编译
    assert '!include "StrFunc.nsh"' in text
    assert "${Using:StrFunc} StrStr" in text
    # 引导式 7z 分卷与覆盖安装语义不能破坏
    assert "RequireVolume 1" in text
    assert 'RMDir /r "$INSTDIR"' not in text.split("Section \"Uninstall\"")[0]


def test_frontend_listens_update_events() -> None:
    text = INDEX.read_text(encoding="utf-8")
    assert 'id="checkUpdate"' in text
    assert 'bridge.invoke("check_update")' in text
    assert 'bridge.invoke("install_update", { info: pendingUpdate })' in text
    assert 'bridge.listen("update-installing"' in text
    assert 'bridge.listen("update-progress"' in text
    assert "pendingUpdate" in text  # 用户二次点击授权（自更新第 3 步）


def test_ci_end_to_end_workflow_covers_seven_steps() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch" in text
    assert "SHA256SUMS" in text
    assert "'/S','/RELAUNCH'" in text
    assert "Assert app auto-relaunched" in text
    assert "$appPid" in text and "$pid =" not in text  # $pid 只读，不能赋值
    assert "--repo \"${{ github.repository }}\"" in text
    assert "actions/checkout@v4" in text
    assert "TripoSR-Lite-Data.7z.*" in text  # 完整包 = Setup + 数据分卷


def test_no_incremental_route_in_repo() -> None:
    """单一路线：不应存在代码增量包 / 死 updater 配置。"""
    assert not (ROOT / "desktop" / "src-tauri" / "src" / "updater_legacy.rs").exists()
    assert not (ROOT / "scripts" / "make_shell_update.ps1").exists()
    assert not (ROOT / ".github" / "workflows" / "shell-update.yml").exists()
