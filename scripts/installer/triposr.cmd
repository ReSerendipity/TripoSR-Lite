@echo off
REM ============================================================================
REM  triposr.cmd - global CLI launcher for the TripoSR-Lite desktop installer
REM ============================================================================
REM  Installed to <InstallDir>\triposr.cmd by setup.nsi and registered on the
REM  current user's PATH, so `triposr ...` works from any terminal and for
REM  AI agents that shell out. Runs the bundled runtime interpreter, NOT the
REM  system Python.
REM
REM  Layout after install (see docs/RELEASE_PROFILE.md):
REM    <InstallDir>\app\runtime\python.exe               (application runtime)
REM    <InstallDir>\app\app\integrated_app\cli.py        (CLI entry)
REM    <InstallDir>\app\external\TripoSR\                (upstream code)
REM    <InstallDir>\app\external\TripoSR\runtime\        (torch/CUDA runtime)
REM    %LOCALAPPDATA%\TripoSR-Lite\models\triposr\       (weights, user supplied)
REM
REM  Keep this file ASCII-only: .cmd is parsed under the active code page.
REM ============================================================================
setlocal EnableExtensions

set "APPROOT=%~dp0app"
set "PYEXE=%APPROOT%\runtime\python.exe"
set "CLIPY=%APPROOT%\app\integrated_app\cli.py"

if not exist "%PYEXE%" (
    echo [triposr] ERROR: bundled runtime not found: %PYEXE% >&2
    exit /b 3
)
if not exist "%CLIPY%" (
    echo [triposr] ERROR: CLI entry not found: %CLIPY% >&2
    exit /b 3
)

REM --- Defaults mirror scripts/desktop_launch.py; explicit env vars win. ---
if not defined LOCALAPPDATA set "LOCALAPPDATA=%USERPROFILE%\AppData\Local"
set "USERDATA=%LOCALAPPDATA%\TripoSR-Lite"
if not defined TRIPOSR_MODEL_DIR set "TRIPOSR_MODEL_DIR=%USERDATA%\models\triposr"
if not defined TRIPOSR_RESULTS_DIR set "TRIPOSR_RESULTS_DIR=%USERDATA%\results"
if not defined TRIPOSR_UPSTREAM_DIR set "TRIPOSR_UPSTREAM_DIR=%APPROOT%\external\TripoSR"
if not defined TRIPOSR_SUPERVISOR set "TRIPOSR_SUPERVISOR=%APPROOT%\scripts\run_triposr_real.py"
if not defined TRIPOSR_PROBE_TIMEOUT_S set "TRIPOSR_PROBE_TIMEOUT_S=300"
if not defined TRIPOSR_PROBE_TTL_S set "TRIPOSR_PROBE_TTL_S=300"
if not defined NUMBA_CACHE_DIR set "NUMBA_CACHE_DIR=%USERDATA%\cache\numba"
if exist "%TRIPOSR_UPSTREAM_DIR%\runtime\python.exe" (
    if not defined TRIPOSR_RUNTIME_PYTHON set "TRIPOSR_RUNTIME_PYTHON=%TRIPOSR_UPSTREAM_DIR%\runtime\python.exe"
)

set "PYTHONNOUSERSITE=1"
set "PYTHONUNBUFFERED=1"
set "HF_HUB_OFFLINE=1"
set "KMP_DUPLICATE_LIB_OK=True"

"%PYEXE%" "%CLIPY%" %*
exit /b %ERRORLEVEL%
