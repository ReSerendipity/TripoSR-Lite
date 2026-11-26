#Requires -Version 5.1
<#
.SYNOPSIS
  TripoSR-Lite 桌面版完整构建：runtime → cargo 门禁 → staging → 冒烟 → 数据分卷 → NSIS → SHA256 → 安装/卸载验收。

.DESCRIPTION
  所有 target/staging/分卷/临时解包落 D 盘构建根（默认 D:\R5Build\TripoSR-Lite）：
    cargo-target\   CARGO_TARGET_DIR
    tmp\            TEMP/TMP
    runtime\        应用 runtime + 外部推理 runtime
    staging\        app\ + 冒烟用壳 exe
    dist\           Setup / Data 分卷 / SHA256SUMS / upload-list.json
    smoke\          staging 冒烟日志与模型目录
    logs\           每步构建日志

  门禁（开关跳过会在报告里如实标注）：
    版本三件套、runtime 导入、cargo test / clippy -D warnings / build --release
    staging 直启 → /api/health + /api/model → 关窗退出清理
    NSIS 静默安装 → 启动 → 健康 → 静默卸载 → 目录/HKCU/快捷方式/数据保留
    7z t 回读、SHA256（LF）、Setup/壳 ProductVersion
#>
[CmdletBinding()]
param(
    [string]$Version = '',
    [string]$Root = '',
    [string]$BuildRoot = 'D:\R5Build\TripoSR-Lite',
    [string]$AppBasePython = '',
    [string]$AppVenvDir = '',
    [string]$ExtBasePython = '',
    [string]$ExtVenvDir = '',
    [string]$SevenZipStandalone = '',
    [string]$MaxPartBytes = '1900MB',
    [int]$Level = 5,
    [switch]$SkipCargoGates,
    [switch]$SkipSmoke,
    [switch]$SkipInstaller,
    [switch]$SkipInstallTest,
    [switch]$ReuseRuntime
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) { Write-Host "`n$Text" -ForegroundColor Yellow }
function Format-Size([long]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}
function Convert-ToBytes([string]$Text) {
    if ($Text -match '^\s*(\d+(?:\.\d+)?)\s*(MB|GB|B)?\s*$') {
        $value = [double]$Matches[1]
        switch ($Matches[2]) {
            'GB' { return [long]($value * 1GB) }
            'MB' { return [long]($value * 1MB) }
            default { return [long]$value }
        }
    }
    return [long]$Text
}
# Windows PowerShell 5.1 在 $ErrorActionPreference='Stop' 下会把原生命令的 stderr
# 变成 NativeCommandError 终止脚本（cargo/torch 的告警都会命中）。统一在局部
# 降为 Continue 捕获，仍按退出码判定失败，输出转字符串避免 ErrorRecord 外溢。
function Invoke-NativeCapture {
    param([string]$Exe, [string[]]$Arguments)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $Exe @Arguments 2>&1 | ForEach-Object { [string]$_ })
    } finally {
        $ErrorActionPreference = $prev
    }
    $code = $LASTEXITCODE
    return [pscustomobject]@{ Code = $code; Output = $output }
}
function Remove-TreeFast([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = (Resolve-Path -LiteralPath $Path).Path
    if ($full.Length -lt 12) { throw "拒绝删除可疑路径：$full" }
    $null = Invoke-NativeCapture 'cmd' @('/c', "rmdir /s /q `"$full`"")
}
function Copy-TreeBulk([string]$Source, [string]$Dest, [string[]]$ExcludeDirs = @(), [string[]]$ExcludeFiles = @()) {
    if (-not (Test-Path -LiteralPath $Source)) { throw "源不存在：$Source" }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    $rcArgs = @($Source, $Dest, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
    if ($ExcludeDirs.Count -gt 0) { $rcArgs += '/XD'; $rcArgs += $ExcludeDirs }
    if ($ExcludeFiles.Count -gt 0) { $rcArgs += '/XF'; $rcArgs += $ExcludeFiles }
    $r = Invoke-NativeCapture 'robocopy' $rcArgs
    if ($r.Code -ge 8) { throw "robocopy 失败（$($r.Code)）：$($r.Output | Select-Object -Last 5)" }
}
function Get-TreeStats([string]$Path) {
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = [long]0
    foreach ($f in $files) { $bytes += $f.Length }
    return [pscustomobject]@{ Files = $files.Count; Bytes = $bytes }
}
function Invoke-Logged([string]$Name, [string]$LogDir, [string]$Exe, [string[]]$Arguments) {
    $log = Join-Path $LogDir "$Name.log"
    $r = Invoke-NativeCapture $Exe $Arguments
    $r.Output | Tee-Object -FilePath $log | Out-Null
    return $r
}
function Find-Standalone7za([string]$Explicit) {
    $candidates = @(
        $Explicit,
        $env:TRIPOSR_7ZA,
        'C:\Program Files\7-Zip\7za.exe',
        'C:\Program Files (x86)\7-Zip\7za.exe'
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c)) { return (Resolve-Path -LiteralPath $c).Path }
    }
    throw ('未找到独立版 7za.exe。NSIS 内嵌工具必须是单文件版（7z.exe 依赖 7z.dll，嵌入后会静默失败）；' +
        '请用 -SevenZipStandalone <path> 指定。')
}
function Get-ShellVersion([string]$ExePath) {
    return (Get-Item -LiteralPath $ExePath).VersionInfo.ProductVersion
}
# 不依赖 Get-FileHash（Windows PowerShell 5.1 会话里该模块函数可能不可用）；
# 直接用 .NET 流式计算，1.9GB 分卷也不会整文件进内存。
function Get-Sha256([string]$Path) {
    $stream = [System.IO.File]::OpenRead($Path)
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        try { $hashBytes = $sha.ComputeHash($stream) } finally { $sha.Dispose() }
    } finally { $stream.Dispose() }
    return ([BitConverter]::ToString($hashBytes) -replace '-', '').ToLowerInvariant()
}
function Get-PyvenvHome([string]$VenvDir) {
    $cfg = Join-Path $VenvDir 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $cfg)) { throw "找不到 pyvenv.cfg：$cfg" }
    $line = (Get-Content -LiteralPath $cfg -Encoding UTF8 | Where-Object { $_ -match '^home\s*=' } | Select-Object -First 1)
    if (-not $line) { throw "pyvenv.cfg 缺少 home 行：$cfg" }
    return ($line -split '=', 2)[1].Trim()
}
function Build-Runtime([string]$BasePython, [string]$SitePackages, [string]$Dest, [string]$Label) {
    Write-Host "    · 组装 $Label runtime：$Dest"
    Remove-TreeFast $Dest
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    Copy-TreeBulk $BasePython $Dest `
        -ExcludeDirs @(
            (Join-Path $BasePython 'Lib\site-packages'),
            (Join-Path $BasePython 'include'),
            (Join-Path $BasePython 'libs'),
            (Join-Path $BasePython 'tcl'),
            (Join-Path $BasePython 'Doc'),
            (Join-Path $BasePython 'share'),
            (Join-Path $BasePython 'Lib\test'),
            (Join-Path $BasePython 'Lib\tkinter'),
            (Join-Path $BasePython 'Lib\turtledemo'),
            (Join-Path $BasePython 'Lib\idlelib')
        ) `
        -ExcludeFiles @('*.pdb', '*.lib', '*.exp')
    $targetSite = Join-Path $Dest 'Lib\site-packages'
    Remove-TreeFast $targetSite
    Copy-TreeBulk $SitePackages $targetSite -ExcludeDirs @('__pycache__') -ExcludeFiles @('*.pyc')
    $runtimePython = Join-Path $Dest 'python.exe'
    if (-not (Test-Path -LiteralPath $runtimePython)) { throw "$Label runtime 缺少 python.exe：$Dest" }
}

# ---------------------------------------------------------------- 环境 ----
if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not $AppVenvDir) { $AppVenvDir = Join-Path $Root '.venv' }
if (-not $ExtVenvDir) { $ExtVenvDir = Join-Path $Root 'external\TripoSR\.venv' }
if (-not $AppBasePython) { $AppBasePython = Get-PyvenvHome $AppVenvDir }
if (-not $ExtBasePython) { $ExtBasePython = Get-PyvenvHome $ExtVenvDir }
$AppBasePython = (Resolve-Path -LiteralPath $AppBasePython).Path
$ExtBasePython = (Resolve-Path -LiteralPath $ExtBasePython).Path
foreach ($p in @($AppBasePython, $ExtBasePython)) {
    if (-not (Test-Path -LiteralPath (Join-Path $p 'python.exe'))) { throw "base Python 缺少 python.exe：$p" }
}

if (-not $Version) {
    $match = [regex]::Match((Get-Content -LiteralPath (Join-Path $Root 'triposr_lite\settings.py') -Raw -Encoding UTF8), '(?m)^VERSION\s*=\s*"([^"]+)"')
    if (-not $match.Success) { throw '无法从 triposr_lite/settings.py 解析版本号' }
    $Version = $match.Groups[1].Value
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "版本号必须是三段数字：$Version" }

$BuildRoot = [System.IO.Path]::GetFullPath($BuildRoot)
if ($BuildRoot -notmatch '^(D:\\|D:/)') { throw "构建根必须在 D 盘（当前 $BuildRoot）" }
$targetDir = Join-Path $BuildRoot 'cargo-target'
$tempDir = Join-Path $BuildRoot 'tmp'
$runtimeRoot = Join-Path $BuildRoot 'runtime'
$appRuntimeDir = Join-Path $runtimeRoot 'app'
$extRuntimeDir = Join-Path $runtimeRoot 'external'
$stagingDir = Join-Path $BuildRoot 'staging'
$distDir = Join-Path $BuildRoot 'dist'
$logDir = Join-Path $BuildRoot 'logs'
$smokeDir = Join-Path $BuildRoot 'smoke'
foreach ($dir in @($targetDir, $tempDir, $runtimeRoot, $stagingDir, $distDir, $logDir, $smokeDir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}

$env:CARGO_TARGET_DIR = $targetDir
$env:TEMP = $tempDir
$env:TMP = $tempDir
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONUNBUFFERED = '1'

Write-Host '=============================================='
Write-Host " TripoSR-Lite 桌面版构建 v$Version"
Write-Host " 仓库：$Root"
Write-Host " 构建根：$BuildRoot"
Write-Host '=============================================='

# ------------------------------------------------------- 版本三件套 ----
Write-Step '[门禁] 版本三件套与主仓一致'
$pkgVersion = (Get-Content -LiteralPath (Join-Path $Root 'desktop\package.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
$confVersion = (Get-Content -LiteralPath (Join-Path $Root 'desktop\src-tauri\tauri.conf.json') -Raw -Encoding UTF8 | ConvertFrom-Json).version
$cargoVersion = [regex]::Match(
    (Get-Content -LiteralPath (Join-Path $Root 'desktop\src-tauri\Cargo.toml') -Raw -Encoding UTF8),
    '(?m)^version\s*=\s*"([^"]+)"'
).Groups[1].Value
foreach ($v in @($pkgVersion, $confVersion, $cargoVersion)) {
    if ($v -ne $Version) { throw "版本三件套不一致：settings=$Version package.json=$pkgVersion tauri.conf=$confVersion Cargo=$cargoVersion" }
}
Write-Host "    · settings.py / package.json / tauri.conf.json / Cargo.toml = $Version"

# --------------------------------------------------------- runtime ----
Write-Step '[准备] 两套独立 Python runtime（base + venv site-packages）'
if ($ReuseRuntime -and (Test-Path -LiteralPath (Join-Path $appRuntimeDir 'python.exe')) -and (Test-Path -LiteralPath (Join-Path $extRuntimeDir 'python.exe'))) {
    Write-Host '    [复用] 已有两套 runtime（-ReuseRuntime）'
} else {
    Build-Runtime $AppBasePython (Join-Path $AppVenvDir 'Lib\site-packages') $appRuntimeDir '应用'
    Build-Runtime $ExtBasePython (Join-Path $ExtVenvDir 'Lib\site-packages') $extRuntimeDir '外部推理'
}
$appRuntimePython = Join-Path $appRuntimeDir 'python.exe'
$extRuntimePython = Join-Path $extRuntimeDir 'python.exe'
$probeApp = Invoke-NativeCapture $appRuntimePython @('-B', '-c', "import sys, fastapi, uvicorn, trimesh, PIL, numpy, multipart; print('app_runtime_ok', sys.version.split()[0])")
if ($probeApp.Code -ne 0) { throw ("应用 runtime 冒烟失败：" + (($probeApp.Output) -join "`n")) }
Write-Host ("    · " + (($probeApp.Output) -join ' '))
$compatDir = Join-Path $Root 'scripts\_compat'
$env:PYTHONPATH = $compatDir
Push-Location (Join-Path $Root 'external\TripoSR')
try {
    $probeExt = Invoke-NativeCapture $extRuntimePython @('-B', '-c', "import sys, torch, tsr.system; print('ext_runtime_ok', sys.version.split()[0], torch.__version__, torch.cuda.is_available())")
} finally {
    Pop-Location
    Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
}
if ($probeExt.Code -ne 0) { throw ("外部 runtime 冒烟失败：" + (($probeExt.Output) -join "`n")) }
Write-Host ("    · " + (($probeExt.Output) -join ' '))
$rtStats = Get-TreeStats $runtimeRoot
Write-Host ("    · runtime 合计 {0} 文件 / {1}" -f $rtStats.Files, (Format-Size $rtStats.Bytes))

# ------------------------------------------------------- cargo 门禁 ----
if ($SkipCargoGates) {
    Write-Warning '跳过 cargo test/clippy/build（-SkipCargoGates）'
} else {
    $manifestPath = Join-Path $Root 'desktop\src-tauri\Cargo.toml'
    $cargoExe = (Get-Command cargo -ErrorAction SilentlyContinue).Source
    if (-not $cargoExe) { throw '未找到 cargo' }

    Write-Step '[门禁] cargo test'
    $r = Invoke-Logged 'cargo-test' $logDir $cargoExe @('test', '--manifest-path', $manifestPath)
    if ($r.Code -ne 0) { throw ("cargo test 失败（$($r.Code)），日志：$logDir\cargo-test.log`n" + (($r.Output | Select-Object -Last 20) -join "`n")) }

    Write-Step '[门禁] cargo clippy --all-targets -- -D warnings'
    $r = Invoke-Logged 'cargo-clippy' $logDir $cargoExe @('clippy', '--manifest-path', $manifestPath, '--all-targets', '--', '-D', 'warnings')
    if ($r.Code -ne 0) { throw ("cargo clippy 失败（$($r.Code)），日志：$logDir\cargo-clippy.log`n" + (($r.Output | Select-Object -Last 20) -join "`n")) }

    Write-Step '[构建] cargo build --release'
    $r = Invoke-Logged 'cargo-build' $logDir $cargoExe @('build', '--manifest-path', $manifestPath, '--release')
    if ($r.Code -ne 0) { throw ("cargo build --release 失败（$($r.Code)），日志：$logDir\cargo-build.log`n" + (($r.Output | Select-Object -Last 20) -join "`n")) }
}

$shellExe = Join-Path $targetDir 'release\TripoSR-Lite.exe'
if (-not (Test-Path -LiteralPath $shellExe)) { throw "找不到壳产物：$shellExe" }
$shellVersion = Get-ShellVersion $shellExe
if ($shellVersion -ne $Version) { throw "壳 ProductVersion=$shellVersion 与主仓 $Version 不一致" }
Write-Host "    · 壳 ProductVersion = $shellVersion"

# ---------------------------------------------------------- staging ----
Write-Step '[组装] staging（代码 + 上游代码 + 两套 runtime）'
& (Join-Path $PSScriptRoot 'assemble_desktop_staging.ps1') `
    -StagingDir $stagingDir -Version $Version -Root $Root `
    -RuntimeDir $appRuntimeDir -ExternalRuntimeDir $extRuntimeDir
if (-not (Test-Path -LiteralPath (Join-Path $stagingDir 'staging-manifest.json'))) {
    throw 'assemble_desktop_staging.ps1 未产出 staging-manifest.json'
}
Copy-Item -LiteralPath $shellExe -Destination (Join-Path $stagingDir 'TripoSR-Lite.exe') -Force
$stagedShell = Join-Path $stagingDir 'TripoSR-Lite.exe'
if ((Get-ShellVersion $stagedShell) -ne $Version) { throw 'staging 壳 ProductVersion 不一致' }
$stagingStats = Get-TreeStats $stagingDir
$appStats = Get-TreeStats (Join-Path $stagingDir 'app')
Write-Host ("    · staging {0} 文件 / {1}（app {2} 文件 / {3}）" -f $stagingStats.Files, (Format-Size $stagingStats.Bytes), $appStats.Files, (Format-Size $appStats.Bytes))

# ----------------------------------------------------------- 冒烟 ----
if ($SkipSmoke) {
    Write-Warning '跳过 staging 直启冒烟（-SkipSmoke）'
} else {
    Write-Step '[冒烟] staging 直启 → /api/health + /api/model → 关窗退出'
    Remove-TreeFast $smokeDir
    foreach ($sub in @('logs', 'results', 'models\triposr')) {
        New-Item -ItemType Directory -Path (Join-Path $smokeDir $sub) -Force | Out-Null
    }
    $env:TRIPOSR_LOG_DIR = Join-Path $smokeDir 'logs'
    $env:TRIPOSR_MODEL_DIR = Join-Path $smokeDir 'models\triposr'
    $env:TRIPOSR_RESULTS_DIR = Join-Path $smokeDir 'results'
    $env:TRIPOSR_STARTUP_TIMEOUT_SECS = '180'
    $env:TRIPOSR_PROBE_TIMEOUT_S = '300'
    $env:TRIPOSR_PROBE_TTL_S = '300'

    $shellLog = Join-Path $smokeDir 'logs\triposr-lite-shell.log'
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $process = Start-Process -FilePath $stagedShell -WorkingDirectory $stagingDir -PassThru
    $deadline = (Get-Date).AddSeconds(150)
    $readyLine = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if (Test-Path -LiteralPath $shellLog) {
            $readyLine = (Get-Content -LiteralPath $shellLog -Raw -Encoding UTF8 -ErrorAction SilentlyContinue |
                Select-String -Pattern 'Python 服务就绪，端口=(\d+)' | Select-Object -First 1)
            if ($readyLine) { break }
        }
        if ($process.HasExited) { throw "壳提前退出（ExitCode=$($process.ExitCode)），日志：$shellLog" }
    }
    if (-not $readyLine) { throw "150s 内未见「Python 服务就绪」日志：$shellLog" }
    $port = [int]$readyLine.Matches[0].Groups[1].Value

    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 10
    if ($health.status -ne 'ok') { throw "staging /api/health 状态异常：$($health.status)" }
    if ($health.version -ne $Version) { throw "staging /api/health 版本 $($health.version) != $Version" }

    $model = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/model" -TimeoutSec 420
    if ($model.probe.state -ne 'NOT_READY') { throw "staging 无权重时 probe.state 应为 NOT_READY，实际 $($model.probe.state)" }
    $codes = @($model.probe.reasons | ForEach-Object { $_.code })
    foreach ($expect in @('MISSING_CONFIG', 'MISSING_WEIGHTS')) {
        if ($codes -notcontains $expect) { throw "NOT_READY 缺失清单异常（缺 $expect）：$($codes -join ',')" }
    }
    if (-not $model.probe.details.runtime.probe.ok) {
        throw "外部 runtime 探测未通过：$($model.probe.details.runtime.probe | ConvertTo-Json -Depth 4 -Compress)"
    }
    if ($model.probe.details.weights_dir -ne (Join-Path $smokeDir 'models\triposr')) {
        throw "模型目录未采纳环境覆盖：$($model.probe.details.weights_dir)"
    }
    if ($model.probe.details.runtime.python -ne (Join-Path $stagingDir 'app\external\TripoSR\runtime\python.exe')) {
        throw "外部运行时路径异常：$($model.probe.details.runtime.python)"
    }

    $pythonLog = Get-ChildItem -LiteralPath (Join-Path $smokeDir 'logs') -Filter 'desktop_python_*.log' |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $pythonLog) { throw '未生成 desktop_python_*.log' }
    $anchor = Select-String -LiteralPath $pythonLog.FullName -Pattern '监听端口: (\d+)' -Encoding UTF8 | Select-Object -First 1
    if (-not $anchor -or [int]$anchor.Matches[0].Groups[1].Value -ne $port) {
        throw "端口日志锚点不一致：shell=$port，python日志=$($anchor.Line)"
    }

    Write-Host "    · 端口 $port；/api/health ok v$($health.version)；probe=$($model.probe.state)"
    Write-Host "    · torch $($model.probe.details.runtime.probe.torch) / cuda $($model.probe.details.runtime.probe.cuda_available)；缺失 $($codes -join ',')"
    Write-Host "    · shell 日志：$shellLog"
    Write-Host "    · python 日志：$($pythonLog.FullName)"

    # 温和关闭：taskkill 不带 /F 发 WM_CLOSE，让壳走 RunEvent::Exit 自清理
    $null = Invoke-NativeCapture 'taskkill' @('/PID', [string]$process.Id)
    $graceDeadline = (Get-Date).AddSeconds(25)
    while (-not $process.HasExited -and (Get-Date) -lt $graceDeadline) { Start-Sleep -Milliseconds 500 }
    if (-not $process.HasExited) {
        Write-Warning '壳未在 25s 内退出，强制杀进程树'
        $null = Invoke-NativeCapture 'taskkill' @('/PID', [string]$process.Id, '/T', '/F')
    }
    Start-Sleep -Seconds 1

    $orphans = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$stagingDir*" })
    if ($orphans.Count -gt 0) { throw "退出后仍有 $($orphans.Count) 个孤儿 python 进程" }
    $exitLog = Select-String -LiteralPath $shellLog -Pattern 'Python 进程树已停止|TripoSR-Lite 壳已退出' -Encoding UTF8 -ErrorAction SilentlyContinue
    if (-not $exitLog) { Write-Warning '壳日志未出现退出清理锚点（可能被强制终止）' }
    Write-Host '    · 退出后无孤儿 python 进程'
    Remove-Item Env:TRIPOSR_LOG_DIR, Env:TRIPOSR_MODEL_DIR, Env:TRIPOSR_RESULTS_DIR, Env:TRIPOSR_STARTUP_TIMEOUT_SECS, Env:TRIPOSR_PROBE_TIMEOUT_S, Env:TRIPOSR_PROBE_TTL_S, Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue
}

# --------------------------------------------------------- 数据分卷 ----
Write-Step '[清理] 冒烟后再清一次字节码/编译缓存（numba 会写 __pycache__）'
$stagingApp = Join-Path $stagingDir 'app'
$smokeCaches = @(Get-ChildItem -LiteralPath $stagingApp -Recurse -Directory -Filter '__pycache__' -Force -ErrorAction SilentlyContinue)
foreach ($d in $smokeCaches) { Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Get-ChildItem -LiteralPath $stagingApp -Recurse -File -Filter '*.pyc' -Force -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
$leftCaches = @(Get-ChildItem -LiteralPath $stagingApp -Recurse -Directory -Filter '__pycache__' -Force -ErrorAction SilentlyContinue)
if ($leftCaches.Count -gt 0) { throw "冒烟后清理不净：仍有 $($leftCaches.Count) 个 __pycache__" }
if (-not (Test-Path -LiteralPath (Join-Path $stagingDir 'staging-manifest.json'))) {
    throw '清理后 staging 不完整（staging-manifest.json 丢失）'
}
Write-Step '[打包] 7z 数据分卷（只含 staging\app）'
$sevenZip = (Get-Command 7z -ErrorAction SilentlyContinue).Source
if (-not $sevenZip) {
    foreach ($c in @('C:\Program Files\7-Zip\7z.exe', 'C:\Program Files (x86)\7-Zip\7z.exe')) {
        if (Test-Path -LiteralPath $c) { $sevenZip = $c; break }
    }
}
if (-not $sevenZip) { throw '未找到 7z.exe' }
$maxBytes = Convert-ToBytes $MaxPartBytes
if ($maxBytes -gt 1900MB) { throw "单卷上限不得超过 1900MB（当前 $MaxPartBytes）" }

$archiveBase = Join-Path $distDir 'TripoSR-Lite-Data.7z'
$stale = @(Get-ChildItem -LiteralPath $distDir -Filter 'TripoSR-Lite-Data.7z.*' -File -ErrorAction SilentlyContinue)
foreach ($f in $stale) { Remove-Item -LiteralPath $f.FullName -Force }
Push-Location $stagingDir
try {
    $r = Invoke-NativeCapture $sevenZip @('a', '-t7z', "-mx=$Level", "-v${maxBytes}b", $archiveBase, 'app', '-bso0', '-bsp0')
    if ($r.Code -ne 0) { throw ("7z 打包失败（$($r.Code)）：" + (($r.Output) -join "`n")) }
} finally {
    Pop-Location
}
$volumes = @(Get-ChildItem -LiteralPath $distDir -Filter 'TripoSR-Lite-Data.7z.*' -File | Sort-Object Name)
if ($volumes.Count -eq 0) { throw '未生成任何数据分卷' }
$firstVolume = $volumes[0].FullName
$r = Invoke-NativeCapture $sevenZip @('t', $firstVolume, '-bso0', '-bsp0')
if ($r.Code -ne 0) { throw ("7z t 回读失败（$($r.Code)）：" + (($r.Output) -join "`n")) }
foreach ($v in $volumes) {
    if ($v.Length -gt 1900MB) { throw "分卷 $($v.Name) 超过 1900MB" }
    Write-Host ("    · {0} {1}" -f $v.Name, (Format-Size $v.Length))
}
Write-Host '    · 7z t 回读通过'
$r = Invoke-NativeCapture $sevenZip @('l', $firstVolume, '-bso0', '-bsp0')
if ($r.Code -ne 0) { throw "7z l 失败（$($r.Code)）" }
$archiveList = $r.Output
$badHits = @($archiveList | Select-String -Pattern '__pycache__|\.pyc|\.ckpt|\.safetensors')
if ($badHits.Count -gt 0) { throw "数据卷含缓存/权重条目 $($badHits.Count) 项，拒绝出包" }
Write-Host '    · 数据卷无 __pycache__/*.pyc/*.ckpt/*.safetensors'

# ------------------------------------------------------------ NSIS ----
$setupExe = Join-Path $distDir "TripoSR-Lite-Setup-v$Version.exe"
if ($SkipInstaller) {
    Write-Warning '跳过 NSIS 安装器（-SkipInstaller）'
} else {
    Write-Step '[打包] NSIS 当前用户引导安装器'
    $sevenZipStandalone = Find-Standalone7za $SevenZipStandalone
    if ((Split-Path -Leaf $sevenZipStandalone) -ne '7za.exe') {
        throw "内嵌解压器必须是独立版 7za.exe：$sevenZipStandalone"
    }
    $makensis = (Get-Command makensis -ErrorAction SilentlyContinue).Source
    if (-not $makensis) { throw '未找到 makensis（NSIS 3.x）' }
    $icon = Join-Path $Root 'desktop\src-tauri\icons\icon.ico'
    $nsi = Join-Path $PSScriptRoot 'installer\setup.nsi'
    if (-not (Test-Path -LiteralPath $nsi)) { throw "缺少 $nsi" }

    $estKb = [long]($appStats.Bytes / 1024)
    $r = Invoke-NativeCapture $makensis @(
        "/DAPP_VERSION=$Version",
        "/DSRC_DIR=$stagingDir",
        "/DOUT_DIR=$distDir",
        "/DSRC_DATA=$firstVolume",
        "/DSRC_SHELL=$stagedShell",
        "/DSRC_7ZA=$sevenZipStandalone",
        "/DAPP_ICON=$icon",
        "/DDATA_VOLUMES=$($volumes.Count)",
        "/DEST_KB=$estKb",
        $nsi
    )
    $r.Output | Tee-Object -FilePath (Join-Path $logDir 'makensis.log') | Out-Null
    if ($r.Code -ne 0) { throw ("makensis 失败（$($r.Code)），日志：$logDir\makensis.log`n" + (($r.Output | Select-Object -Last 20) -join "`n")) }
    if (-not (Test-Path -LiteralPath $setupExe)) { throw "未生成安装器：$setupExe" }
    $setupVersion = Get-ShellVersion $setupExe
    if ($setupVersion -ne $Version) { throw "安装器 ProductVersion=$setupVersion != $Version" }
    Write-Host ("    · {0} / {1} / ProductVersion {2}" -f (Split-Path -Leaf $setupExe), (Format-Size (Get-Item $setupExe).Length), $setupVersion)
}

# ------------------------------------------------- SHA256 / 清单 ----
Write-Step '[产出] SHA256SUMS 与 upload-list.json'
$artifacts = @()
if (Test-Path -LiteralPath $setupExe) { $artifacts += Get-Item -LiteralPath $setupExe }
$artifacts += $volumes
$sumLines = @()
$upload = @()
foreach ($file in $artifacts) {
    $hash = Get-Sha256 $file.FullName
    $sumLines += "$hash  $($file.Name)"
    $upload += [pscustomobject]@{ file = $file.Name; bytes = $file.Length; sha256 = $hash }
}
# LF 行尾：支持 Git Bash `sha256sum -c`
[System.IO.File]::WriteAllText((Join-Path $distDir 'SHA256SUMS'), (($sumLines -join "`n") + "`n"), [System.Text.Encoding]::ASCII)
# 显式 UTF-8 无 BOM：Windows PowerShell 5.1 的 Set-Content -Encoding utf8 会写 BOM，
# 与既有 upload-list.json（无 BOM，json.load 可直接解析）不一致。
$uploadJson = ($upload | ConvertTo-Json -Depth 4)
[System.IO.File]::WriteAllText((Join-Path $distDir 'upload-list.json'), $uploadJson + "`n", ([System.Text.UTF8Encoding]::new($false)))
$upload | Format-Table -AutoSize | Out-String -Width 220 | Write-Host

$shaTool = 'C:\Program Files\Git\usr\bin\sha256sum.exe'
if (Test-Path -LiteralPath $shaTool) {
    Push-Location $distDir
    try {
        $r = Invoke-NativeCapture $shaTool @('-c', 'SHA256SUMS')
        if ($r.Code -ne 0) { throw ("sha256sum -c 失败：" + (($r.Output) -join "`n")) }
        Write-Host '    · Git Bash sha256sum -c 校验通过'
    } finally { Pop-Location }
} else {
    Write-Warning '未找到 Git Bash sha256sum，跳过外部校验（已用 .NET 计算并写入 LF）'
}

# ---------------------------------------------- 安装 / 卸载验收 ----
if ($SkipInstaller -or $SkipInstallTest) {
    Write-Warning '跳过安装/卸载验收（-SkipInstaller 或 -SkipInstallTest）'
} else {
    Write-Step '[验收] NSIS 静默安装 → 启动 → 健康 → 静默卸载'
    $installDir = Join-Path $env:LOCALAPPDATA 'Programs\TripoSR-Lite'
    $userDataDir = Join-Path $env:LOCALAPPDATA 'TripoSR-Lite'
    $uninstKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TripoSR-Lite'
    $appRegKey = 'HKCU:\Software\TripoSR-Lite'
    $desktopLnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'TripoSR-Lite 单图重建工作台.lnk'
    $startMenuRoot = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'

    if (Test-Path -LiteralPath $installDir) { throw "已存在安装目录，拒绝覆盖验收：$installDir" }
    if (Test-Path -LiteralPath $uninstKey) { throw '已存在 HKCU 卸载键，环境不干净' }

    # 用户数据目录允许已存在（上次验收会按契约保留），本次用哨兵验证「安装/卸载不动用户数据」。
    $preMarker = Join-Path $userDataDir 'pre-existing-keep-check.txt'
    New-Item -ItemType Directory -Path $userDataDir -Force | Out-Null
    "pre-existing-user-data $Version" | Set-Content -LiteralPath $preMarker -Encoding utf8

    $installProc = Start-Process -FilePath $setupExe -ArgumentList '/S' -Wait -PassThru
    if ($installProc.ExitCode -ne 0) { throw "静默安装退出码 $($installProc.ExitCode)" }
    Start-Sleep -Seconds 2

    foreach ($rel in @('TripoSR-Lite.exe', 'app\scripts\desktop_launch.py', 'app\runtime\python.exe', 'app\external\TripoSR\runtime\python.exe', 'app\external\TripoSR\run.py', 'uninstall.exe')) {
        if (-not (Test-Path -LiteralPath (Join-Path $installDir $rel))) { throw "安装后缺少 $rel" }
    }
    if (-not (Test-Path -LiteralPath (Join-Path $userDataDir 'models\triposr'))) { throw '安装后未创建模型目录' }
    $props = Get-ItemProperty -LiteralPath $uninstKey
    if ($props.DisplayName -ne 'TripoSR-Lite 单图重建工作台') { throw "DisplayName 异常：$($props.DisplayName)" }
    if ($props.DisplayVersion -ne $Version) { throw "DisplayVersion $($props.DisplayVersion) != $Version" }
    if ($props.UninstallString -notlike "*uninstall.exe*") { throw "UninstallString 异常：$($props.UninstallString)" }
    if (-not (Test-Path -LiteralPath $desktopLnk)) { throw "缺少桌面快捷方式：$desktopLnk" }
    $startLnks = @(Get-ChildItem -LiteralPath $startMenuRoot -Recurse -Filter '*TripoSR-Lite*.lnk' -File -ErrorAction SilentlyContinue)
    if ($startLnks.Count -eq 0) { throw '缺少开始菜单快捷方式' }
    $installedVersion = Get-ShellVersion (Join-Path $installDir 'TripoSR-Lite.exe')
    if ($installedVersion -ne $Version) { throw "安装后壳 ProductVersion=$installedVersion" }
    Write-Host ("    · 安装目录 {0}；快捷方式 desktop=1 start={1}；DisplayVersion={2}" -f $installDir, $startLnks.Count, $props.DisplayVersion)

    # 保留数据标记：卸载后必须仍在
    $marker = Join-Path $userDataDir 'uninstall-keep-check.txt'
    "keep-data-marker $Version" | Set-Content -LiteralPath $marker -Encoding utf8

    $installedLog = Join-Path $userDataDir 'logs\triposr-lite-shell.log'
    $readyBefore = 0
    if (Test-Path -LiteralPath $installedLog) {
        $readyBefore = @(Get-Content -LiteralPath $installedLog -Tail 200 -Encoding UTF8 -ErrorAction SilentlyContinue |
            Select-String -Pattern 'Python 服务就绪，端口=(\d+)').Count
    }
    $env:TRIPOSR_STARTUP_TIMEOUT_SECS = '240'
    $env:TRIPOSR_PROBE_TIMEOUT_S = '300'
    $env:TRIPOSR_PROBE_TTL_S = '300'
    $appProc = Start-Process -FilePath (Join-Path $installDir 'TripoSR-Lite.exe') -WorkingDirectory $installDir -PassThru
    $deadline = (Get-Date).AddSeconds(240)
    $readyLine = $null
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
        if (Test-Path -LiteralPath $installedLog) {
            $readyHits = @(Get-Content -LiteralPath $installedLog -Tail 200 -Encoding UTF8 -ErrorAction SilentlyContinue |
                Select-String -Pattern 'Python 服务就绪，端口=(\d+)')
            if ($readyHits.Count -gt $readyBefore) {
                $readyLine = $readyHits[-1]
                break
            }
        }
        if ($appProc.HasExited) { throw "安装版壳提前退出（ExitCode=$($appProc.ExitCode)），日志：$installedLog" }
    }
    if (-not $readyLine) { throw "240s 内安装版未见服务就绪日志：$installedLog" }
    $port = [int]$readyLine.Matches[0].Groups[1].Value

    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/health" -TimeoutSec 10
    if ($health.version -ne $Version) { throw "安装版 /api/health 版本 $($health.version) != $Version" }
    $model = Invoke-RestMethod -Uri "http://127.0.0.1:$port/api/model" -TimeoutSec 420
    if ($model.probe.state -ne 'NOT_READY') { throw "安装版无权重时 state=$($model.probe.state)，应为 NOT_READY" }
    if (-not $model.probe.details.runtime.probe.ok) { throw '安装版外部 runtime 探测失败' }
    if ($model.probe.details.weights_dir -ne (Join-Path $userDataDir 'models\triposr')) {
        throw "安装版模型目录异常：$($model.probe.details.weights_dir)"
    }
    if ($model.probe.details.runtime.python -ne (Join-Path $installDir 'app\external\TripoSR\runtime\python.exe')) {
        throw "安装版外部运行时路径异常：$($model.probe.details.runtime.python)"
    }
    Write-Host ("    · 安装版端口 {0}；probe={1}；缺失 {2}" -f $port, $model.probe.state, (($model.probe.reasons | ForEach-Object { $_.code }) -join ','))

    # 页面 NOT_READY / DEMO 标注（headless Edge 截图，可选证据）
    $edge = 'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
    if (Test-Path -LiteralPath $edge) {
        $shot = Join-Path $logDir 'install-ui-notready.png'
        $edgeArgs = @(
            '--headless=new', '--disable-gpu', '--hide-scrollbars',
            "--user-data-dir=$tempDir\edge-shot",
            "--screenshot=$shot", '--window-size=1440,900',
            '--virtual-time-budget=12000',
            "http://127.0.0.1:$port/"
        )
        $null = Invoke-NativeCapture $edge $edgeArgs
        if (Test-Path -LiteralPath $shot) { Write-Host "    · UI 截图：$shot" } else { Write-Warning 'headless Edge 截图未生成' }
    }

    $null = Invoke-NativeCapture 'taskkill' @('/PID', [string]$appProc.Id)
    $graceDeadline = (Get-Date).AddSeconds(25)
    while (-not $appProc.HasExited -and (Get-Date) -lt $graceDeadline) { Start-Sleep -Milliseconds 500 }
    if (-not $appProc.HasExited) {
        Write-Warning '安装版壳未在 25s 内退出，强制杀进程树'
        $null = Invoke-NativeCapture 'taskkill' @('/PID', [string]$appProc.Id, '/T', '/F')
    }
    Start-Sleep -Seconds 1
    $orphans = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*$installDir*" })
    if ($orphans.Count -gt 0) { throw "安装版退出后仍有 $($orphans.Count) 个孤儿 python 进程" }
    Remove-Item Env:TRIPOSR_STARTUP_TIMEOUT_SECS, Env:TRIPOSR_PROBE_TIMEOUT_S, Env:TRIPOSR_PROBE_TTL_S -ErrorAction SilentlyContinue

    # 静默卸载（默认保留用户数据）
    $uninstaller = Join-Path $installDir 'uninstall.exe'
    if (-not (Test-Path -LiteralPath $uninstaller)) { throw '缺少 uninstall.exe' }
    $unProc = Start-Process -FilePath $uninstaller -ArgumentList '/S' -Wait -PassThru
    if ($unProc.ExitCode -ne 0) { throw "静默卸载退出码 $($unProc.ExitCode)" }
    $unDeadline = (Get-Date).AddSeconds(90)
    while ((Test-Path -LiteralPath (Join-Path $installDir 'TripoSR-Lite.exe')) -and (Get-Date) -lt $unDeadline) { Start-Sleep -Seconds 1 }
    if (Test-Path -LiteralPath (Join-Path $installDir 'TripoSR-Lite.exe')) { throw '卸载后壳 exe 仍在' }
    if (Test-Path -LiteralPath $uninstKey) { throw '卸载后 HKCU 卸载键仍在' }
    if (Test-Path -LiteralPath $appRegKey) { throw '卸载后 HKCU 应用键仍在' }
    if (Test-Path -LiteralPath $desktopLnk) { throw '卸载后桌面快捷方式仍在' }
    $startLeft = @(Get-ChildItem -LiteralPath $startMenuRoot -Recurse -Filter '*TripoSR-Lite*.lnk' -File -ErrorAction SilentlyContinue)
    if ($startLeft.Count -gt 0) { throw "卸载后开始菜单快捷方式仍在：$($startLeft[0].FullName)" }
    if (-not (Test-Path -LiteralPath $marker)) { throw '静默卸载未保留用户数据（marker 丢失）' }
    if (-not (Test-Path -LiteralPath $preMarker)) { throw '静默卸载改动了既有用户数据（pre-marker 丢失）' }
    if (-not (Test-Path -LiteralPath (Join-Path $userDataDir 'models\triposr'))) { throw '卸载后模型目录未保留' }
    Write-Host ("    · 卸载后：程序目录已移除、HKCU/快捷方式已清理、用户数据保留（{0}）" -f $userDataDir)
    Remove-Item -LiteralPath $marker, $preMarker -Force
}

# ------------------------------------------------ 结尾清理与汇总 ----
Remove-TreeFast (Join-Path $tempDir 'edge-shot')
Remove-TreeFast $tempDir
Write-Host ''
Write-Host ("[完成] v{0} 产物目录：{1}" -f $Version, $distDir) -ForegroundColor Green
Get-ChildItem -LiteralPath $distDir | Select-Object Name, Length | Format-Table -AutoSize | Out-String -Width 120 | Write-Host
