"""S1 安装器契约：PATH 增删算法边界 + shim/NSIS 静态约定。

Windows 沙箱里 cmd.exe 不可直接调用，.cmd 逻辑以等价 Python 实现验证；
NSIS 段以静态契约钉住，编译由 makensis 命令单独验收。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NSI = ROOT / "scripts" / "installer" / "setup.nsi"
CMD = ROOT / "scripts" / "installer" / "triposr.cmd"
INSTDIR = r"C:\Users\Doro\AppData\Local\Programs\TripoSR-Lite"


def add_to_user_path(path: str, instdir: str = INSTDIR) -> str:
    """NSIS AddToUserPath 的等价实现（整段 ";x;" 匹配）。"""
    needle = f";{instdir};"
    if needle in f";{path};":
        return path
    if not path:
        return instdir
    return path + ("" if path.endswith(";") else ";") + instdir


def remove_from_user_path(path: str, instdir: str = INSTDIR) -> str:
    """NSIS un.RemoveFromUserPath 的等价实现。"""
    needle = f";{instdir};"
    wrapped = f";{path};"
    if needle not in wrapped:
        return path
    replaced = wrapped.replace(needle, ";").replace(needle, ";")
    while ";;" in replaced:
        replaced = replaced.replace(";;", ";")
    return replaced.strip(";")


@pytest.mark.parametrize(
    ("initial", "expected"),
    [
        ("", INSTDIR),                                             # 1 空 PATH
        (r"C:\Windows", rf"C:\Windows;{INSTDIR}"),                 # 2 无尾分隔符
        (r"C:\Windows;", rf"C:\Windows;{INSTDIR}"),                # 3 已有尾分隔符
        (INSTDIR, INSTDIR),                                        # 4 已存在，幂等
        (rf"{INSTDIR};C:\Windows", rf"{INSTDIR};C:\Windows"),      # 5 位于开头，幂等
        (rf"C:\A;{INSTDIR};C:\B", rf"C:\A;{INSTDIR};C:\B"),        # 6 位于中间，幂等
        (INSTDIR + r"Extra", INSTDIR + "Extra;" + INSTDIR),        # 7 前缀相似仍是不同条目
        (r"C:\NotTheInstallDir", rf"C:\NotTheInstallDir;{INSTDIR}"),  # 8 无关路径
    ],
)
def test_add_is_idempotent_and_exact(initial: str, expected: str) -> None:
    once = add_to_user_path(initial)
    assert once == expected
    assert add_to_user_path(once) == once


@pytest.mark.parametrize(
    ("initial", "expected"),
    [
        (INSTDIR, ""),                                             # 1 唯一条目
        (rf"C:\A;{INSTDIR}", r"C:\A"),                             # 2 末尾条目
        (rf"{INSTDIR};C:\B", r"C:\B"),                             # 3 开头条目
        (rf"C:\A;{INSTDIR};C:\B", r"C:\A;C:\B"),                   # 4 中间条目
        (rf"{INSTDIR};{INSTDIR}", ""),                             # 5 重复条目全清
        (r"C:\A;C:\B", r"C:\A;C:\B"),                              # 6 不存在，不变
        (INSTDIR + r"Extra", INSTDIR + "Extra"),                   # 7 前缀相似不误删
        ("", ""),                                                  # 8 空 PATH
    ],
)
def test_remove_is_exact_and_idempotent(initial: str, expected: str) -> None:
    once = remove_from_user_path(initial)
    assert once == expected
    assert remove_from_user_path(once) == once


def test_add_remove_roundtrip() -> None:
    for initial in ("", r"C:\Windows", r"C:\Windows;"):
        assert remove_from_user_path(add_to_user_path(initial)) == initial.rstrip(";")
    # 已含安装目录的 PATH 不会被 add 改动，remove 后该条目被精确摘除。
    assert remove_from_user_path(add_to_user_path(rf"C:\A;{INSTDIR};C:\B")) == r"C:\A;C:\B"


def test_nsis_static_contract() -> None:
    text = NSI.read_text(encoding="utf-8-sig")
    assert '!include "StrFunc.nsh"' in text
    assert "${Using:StrFunc} StrStr" in text
    assert "${Using:StrFunc} UnStrStr" in text
    assert "${Using:StrFunc} UnStrRep" in text
    assert "${Using:StrFunc} StrRep" not in text  # 未使用，避免 warning 6010
    assert "Function AddToUserPath" in text
    assert "Function un.RemoveFromUserPath" in text
    assert '  Call AddToUserPath' in text
    assert "  Call un.RemoveFromUserPath" in text
    assert '  File "triposr.cmd"' in text
    assert "SendMessage ${HWND_BROADCAST} ${WM_WININICHANGE} 0 \"STR:Environment\"" in text
    assert 'CreateShortcut "$SMPROGRAMS\\${APP_DISPLAY_NAME}\\TripoSR-Lite CLI.lnk" "$SYSDIR\\cmd.exe"' in text
    assert '  Delete "$INSTDIR\\triposr.cmd"' in text
    # 引导式 7z 分卷结构不得被 S1 破坏
    assert "RequireVolume 1" in text
    assert "7za.exe" in text
    assert 'RMDir /r "$INSTDIR"' in text


def test_shim_is_ascii_and_has_contract() -> None:
    raw = CMD.read_bytes()
    raw.decode("ascii")  # .cmd 按活动代码页解析，必须 ASCII-only
    text = raw.decode("ascii")
    assert 'set "APPROOT=%~dp0app"' in text
    assert 'set "PYEXE=%APPROOT%\\runtime\\python.exe"' in text
    assert 'set "CLIPY=%APPROOT%\\app\\integrated_app\\cli.py"' in text
    assert '"%PYEXE%" "%CLIPY%" %*' in text
    assert "exit /b %ERRORLEVEL%" in text
    assert "PYTHONNOUSERSITE=1" in text
