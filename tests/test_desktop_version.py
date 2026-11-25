"""桌面壳版本三件套与主仓单一事实源一致性测试。

发版断言的根基：`triposr_lite/settings.py` 的 VERSION 是唯一事实源，
`desktop/package.json` / `desktop/src-tauri/tauri.conf.json` /
`desktop/src-tauri/Cargo.toml` 都是 git 跟踪文件，任何一处漏改这里立刻红。
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from triposr_lite import settings

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _cargo() -> dict:
    return tomllib.loads((DESKTOP / "src-tauri" / "Cargo.toml").read_text(encoding="utf-8"))


def test_shell_versions_match_server_version():
    assert _json(DESKTOP / "package.json")["version"] == settings.VERSION
    assert _json(DESKTOP / "src-tauri" / "tauri.conf.json")["version"] == settings.VERSION
    assert _cargo()["package"]["version"] == settings.VERSION


def test_shell_product_identity_is_stable():
    conf = _json(DESKTOP / "src-tauri" / "tauri.conf.json")
    cargo = _cargo()
    assert conf["productName"] == "TripoSR-Lite"
    assert conf["identifier"] == "com.triposr.lite"
    assert cargo["package"]["name"] == "triposr-lite-desktop"
    assert cargo["bin"][0]["name"] == "TripoSR-Lite"


def test_release_profile_documents_contract_endpoints():
    text = (ROOT / "docs" / "RELEASE_PROFILE.md").read_text(encoding="utf-8")
    assert "/api/health" in text
    assert "/api/model" in text
    assert "TRIPOSR_MODEL_DIR" in text
    assert "TripoSR-Lite-Data.7z.001" in text
