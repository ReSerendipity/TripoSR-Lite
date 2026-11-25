#Requires -Version 5.1
<#
.SYNOPSIS
  TripoSR-Lite 桌面版 staging 组装：git 跟踪代码 + 上游代码 + 两套指定 runtime。

.DESCRIPTION
  产物布局（staging 根 = 安装根，壳按 <exe_dir>\app\scripts\desktop_launch.py 判定）：
    <StagingDir>\app\
      triposr_lite\ scripts\ docs\
      external\TripoSR\{run.py,tsr,LICENSE,...}\runtime\
      runtime\python.exe ...
      models\triposr\MODEL_SETUP_GUIDE.md   # 空模型目录，不含权重
      settings.py 经 triposr_lite\ 随包；README/LICENSE/NOTICE 等随包

  硬约束：
  - 主仓代码只来自 `git archive HEAD`（工作树必须先提交，保证 staging 与 commit 一致）。
  - 上游 external/TripoSR 代码与 .venv 属「指定 runtime」，从本机 checkout 复制，排除
    .git/.venv/examples/figures/缓存；两套 runtime 由 -RuntimeDir/-ExternalRuntimeDir 指定。
  - models/ 权重、results/、.venv/、__pycache__、*.pyc、*.ckpt、*.safetensors 一律不进 staging。
  - 归档用 Windows System32\tar.exe，不用 MSYS tar。
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$StagingDir,
    [string]$Version = '',
    [string]$Root = '',
    [string]$RuntimeDir = '',
    [string]$ExternalRuntimeDir = '',
    [string]$ExternalCodeDir = '',
    [switch]$SkipRuntime
)

$ErrorActionPreference = 'Stop'

function Write-Step([string]$Text) { Write-Host "`n$Text" -ForegroundColor Yellow }
function Format-Size([long]$Bytes) {
    if ($Bytes -ge 1GB) { return ('{0:N2} GB' -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ('{0:N1} MB' -f ($Bytes / 1MB)) }
    return ('{0:N0} KB' -f ($Bytes / 1KB))
}
function Remove-TreeFast([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $full = (Resolve-Path -LiteralPath $Path).Path
    if ($full.Length -lt 12) { throw "拒绝删除可疑路径：$full" }
    & cmd /c "rmdir /s /q `"$full`"" 2>$null | Out-Null
}
function Copy-TreeBulk([string]$Source, [string]$Dest, [string[]]$ExcludeDirs = @(), [string[]]$ExcludeFiles = @()) {
    if (-not (Test-Path -LiteralPath $Source)) { throw "源不存在：$Source" }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    $args = @($Source, $Dest, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
    if ($ExcludeDirs.Count -gt 0) { $args += '/XD'; $args += $ExcludeDirs }
    if ($ExcludeFiles.Count -gt 0) { $args += '/XF'; $args += $ExcludeFiles }
    $out = & robocopy @args 2>&1
    if ($LASTEXITCODE -ge 8) { throw "robocopy 失败（$LASTEXITCODE）：$($out | Select-Object -Last 5)" }
}
function Get-TreeStats([string]$Path) {
    $files = @(Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue)
    $bytes = [long]0
    foreach ($f in $files) { $bytes += $f.Length }
    return [pscustomobject]@{ Files = $files.Count; Bytes = $bytes }
}

if (-not $Root) { $Root = Split-Path -Parent $PSScriptRoot }
$Root = (Resolve-Path -LiteralPath $Root).Path
if (-not $ExternalCodeDir) { $ExternalCodeDir = Join-Path $Root 'external\TripoSR' }

if (-not $Version) {
    $settingsPy = Join-Path $Root 'triposr_lite\settings.py'
    $match = [regex]::Match((Get-Content -LiteralPath $settingsPy -Raw -Encoding UTF8), '(?m)^VERSION\s*=\s*"([^"]+)"')
    if (-not $match.Success) { throw '无法从 triposr_lite/settings.py 解析版本号' }
    $Version = $match.Groups[1].Value
}

Write-Host '=============================================='
Write-Host " TripoSR-Lite staging 组装 v$Version"
Write-Host " staging：$StagingDir"
Write-Host '=============================================='

$appDir = Join-Path $StagingDir 'app'
Remove-TreeFast $StagingDir
New-Item -ItemType Directory -Path $appDir -Force | Out-Null

# ------------------------------------------------------------ 代码归档 ----
Write-Step '[归档] git archive HEAD（只含跟踪代码）'
$tarPath = Join-Path $env:TEMP ("triposr-stage-{0}.tar" -f [guid]::NewGuid().ToString('N'))
$archivePaths = @(
    'triposr_lite',
    'scripts',
    'docs',
    'README.md',
    'LICENSE',
    'NOTICE',
    'THIRD_PARTY_NOTICES.md',
    'THIRD_PARTY_LICENSES',
    'requirements.txt',
    'requirements-dev.txt',
    'start-local.cmd'
)
& git -C $Root archive --format=tar --output=$tarPath HEAD -- @archivePaths
if ($LASTEXITCODE -ne 0) { throw "git archive 失败（$LASTEXITCODE）" }
& "$env:SystemRoot\System32\tar.exe" -xf $tarPath -C $appDir
if ($LASTEXITCODE -ne 0) { throw "解包 git archive 失败（$LASTEXITCODE）" }
Remove-Item -LiteralPath $tarPath -Force

$launch = Join-Path $appDir 'scripts\desktop_launch.py'
if (-not (Test-Path -LiteralPath $launch)) { throw 'staging 缺少 scripts/desktop_launch.py' }
$compat = Join-Path $appDir 'scripts\_compat\torchmcubes\__init__.py'
if (-not (Test-Path -LiteralPath $compat)) { throw 'staging 缺少 scripts/_compat/torchmcubes' }

$stagedSettings = Get-Content -LiteralPath (Join-Path $appDir 'triposr_lite\settings.py') -Raw -Encoding UTF8
$match = [regex]::Match($stagedSettings, '(?m)^VERSION\s*=\s*"([^"]+)"')
if (-not $match.Success -or $match.Groups[1].Value -ne $Version) {
    throw "staging settings.py 版本与 $Version 不一致"
}

# ------------------------------------------------------- 上游代码（指定 runtime） ----
Write-Step '[归档] external/TripoSR 上游代码（run.py + tsr/）'
$externalDst = Join-Path $appDir 'external\TripoSR'
New-Item -ItemType Directory -Path $externalDst -Force | Out-Null
foreach ($f in @('run.py', 'LICENSE', 'README.md', 'requirements.txt')) {
    $src = Join-Path $ExternalCodeDir $f
    if (-not (Test-Path -LiteralPath $src)) { throw "上游代码缺少 $f：$src" }
    Copy-Item -LiteralPath $src -Destination (Join-Path $externalDst $f) -Force
}
Copy-TreeBulk (Join-Path $ExternalCodeDir 'tsr') (Join-Path $externalDst 'tsr') `
    -ExcludeDirs @('__pycache__') -ExcludeFiles @('*.pyc')
if (-not (Test-Path -LiteralPath (Join-Path $externalDst 'run.py'))) { throw 'staging 缺少 external/TripoSR/run.py' }
if (-not (Test-Path -LiteralPath (Join-Path $externalDst 'tsr\system.py'))) { throw 'staging 缺少 external/TripoSR/tsr' }

# ---------------------------------------------------------------- runtime ----
Write-Step '[归档] 两套指定 Python runtime'
if ($SkipRuntime) {
    Write-Warning '跳过 runtime 归档（-SkipRuntime，仅开发冒烟）'
} else {
    if (-not $RuntimeDir) { throw '必须传 -RuntimeDir（应用运行时目录，含 python.exe）' }
    if (-not $ExternalRuntimeDir) { throw '必须传 -ExternalRuntimeDir（外部推理运行时目录，含 python.exe）' }
    $RuntimeDir = (Resolve-Path -LiteralPath $RuntimeDir).Path
    $ExternalRuntimeDir = (Resolve-Path -LiteralPath $ExternalRuntimeDir).Path
    foreach ($pair in @(@($RuntimeDir, (Join-Path $appDir 'runtime')), @($ExternalRuntimeDir, (Join-Path $externalDst 'runtime')))) {
        $src = $pair[0]; $dst = $pair[1]
        if (-not (Test-Path -LiteralPath (Join-Path $src 'python.exe'))) { throw "runtime 缺少 python.exe：$src" }
        Copy-TreeBulk $src $dst -ExcludeDirs @('__pycache__') -ExcludeFiles @('*.pyc')
        if (-not (Test-Path -LiteralPath (Join-Path $dst 'python.exe'))) { throw "runtime 复制后缺少 python.exe：$dst" }
    }
    $appPy = Join-Path $appDir 'runtime\python.exe'
    $extPy = Join-Path $externalDst 'runtime\python.exe'
    $probeApp = & $appPy -B -c "import sys, fastapi, uvicorn, trimesh, PIL, numpy, multipart; print('app_runtime_ok', sys.version.split()[0])" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "应用 runtime 冒烟失败：$probeApp" }
    Write-Host "    · $probeApp"
    $probeExt = & $extPy -B -c "import sys, torch; print('ext_runtime_ok', sys.version.split()[0], torch.__version__, torch.cuda.is_available())" 2>&1
    if ($LASTEXITCODE -ne 0) { throw "外部 runtime 冒烟失败：$probeExt" }
    Write-Host "    · $probeExt"
}

# ------------------------------------------------- 模型目录（空目录 + 指南） ----
$modelDst = Join-Path $appDir 'models\triposr'
New-Item -ItemType Directory -Path $modelDst -Force | Out-Null
@'
# 模型放置说明（TripoSR-Lite）

本安装包不包含模型权重。需要在同一目录放入：

- config.yaml
- model.ckpt（官方约 1.68 GB）

安装态目录：%LOCALAPPDATA%\TripoSR-Lite\models\triposr
（应用会自动创建并注入 TRIPOSR_MODEL_DIR；加载页「打开模型目录」可直达）

下载命令（官方 HuggingFace 仓库 stabilityai/TripoSR）：
  huggingface-cli download stabilityai/TripoSR --local-dir models/triposr --include config.yaml model.ckpt

放入后重启应用；未放入时界面显示 NOT_READY 与 DEMO 演示网格，不会伪造推理。
详细说明见 app/docs/MODELS.md。
'@ | Set-Content -LiteralPath (Join-Path $modelDst 'MODEL_SETUP_GUIDE.md') -Encoding utf8

# ------------------------------------------------------------ 缓存清理 ----
Write-Step '[清理] 字节码缓存'
$cacheDirs = @(Get-ChildItem -LiteralPath $appDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
foreach ($d in $cacheDirs) { Remove-Item -LiteralPath $d.FullName -Recurse -Force -ErrorAction SilentlyContinue }
Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
# onnxruntime 自带的教程样例 ONNX（几百字节）纯属噪声，删掉以免与「模型」混淆
foreach ($pyRoot in @((Join-Path $appDir 'runtime'), (Join-Path $externalDst 'runtime'))) {
    $ortSamples = Join-Path $pyRoot 'Lib\site-packages\onnxruntime\datasets'
    if (Test-Path -LiteralPath $ortSamples) {
        Get-ChildItem -LiteralPath $ortSamples -Filter '*.onnx' -File | Remove-Item -Force -ErrorAction SilentlyContinue
    }
}

# ------------------------------------------------------- 权重零容忍 ----
Write-Step '[校验] 权重、数据与缓存零容忍'
foreach ($p in @('results', '.venv', '.pytest_cache')) {
    if (Test-Path -LiteralPath (Join-Path $appDir $p)) { throw "staging 内出现 $p" }
}
$leftCaches = @(Get-ChildItem -LiteralPath $appDir -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue)
if ($leftCaches.Count -gt 0) { throw "staging 内仍有 $($leftCaches.Count) 个 __pycache__ 目录" }
$leftPyc = @(Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter '*.pyc' -ErrorAction SilentlyContinue)
if ($leftPyc.Count -gt 0) { throw "staging 内仍有 $($leftPyc.Count) 个 .pyc 文件" }

$modelHits = @(Get-ChildItem -LiteralPath $appDir -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Extension -in @('.ckpt', '.safetensors') -or $_.Name -eq 'model.ckpt' })
if ($modelHits.Count -gt 0) {
    throw ("staging 内出现模型权重：" + (($modelHits | Select-Object -First 5).FullName -join '; '))
}
$projectModels = @(Get-ChildItem -LiteralPath (Join-Path $appDir 'models') -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -ne 'MODEL_SETUP_GUIDE.md' })
if ($projectModels.Count -gt 0) {
    throw ("models/ 内出现非指南文件：" + (($projectModels | Select-Object -First 5).FullName -join '; '))
}
$onnxHits = @(Get-ChildItem -LiteralPath $appDir -Recurse -File -Filter '*.onnx' -ErrorAction SilentlyContinue)
if ($onnxHits.Count -gt 0) {
    throw ("staging 内出现 .onnx：" + (($onnxHits | Select-Object -First 5).FullName -join '; '))
}

# ---------------------------------------------------------------- 统计 ----
$codeStats = Get-TreeStats (Join-Path $appDir 'triposr_lite')
$allStats = Get-TreeStats $appDir
Write-Host ''
Write-Host ("[统计] app 合计 {0} 文件 / {1}" -f $allStats.Files, (Format-Size $allStats.Bytes))
Write-Host ("       其中 triposr_lite/ {0} 文件 / {1}" -f $codeStats.Files, (Format-Size $codeStats.Bytes))

# ------------------------------------------------------------ manifest ----
$gitHead = (& git -C $Root rev-parse HEAD).Trim()
$manifest = [pscustomobject]@{
    schema         = 1
    product        = 'TripoSR-Lite-Desktop'
    app_version    = $Version
    git_head       = $gitHead
    arch           = 'win-x64'
    created_utc    = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    staging_dir    = $StagingDir
    app_files      = $allStats.Files
    app_bytes      = $allStats.Bytes
    app_runtime    = if ($SkipRuntime) { '(skipped)' } else { $RuntimeDir }
    ext_runtime    = if ($SkipRuntime) { '(skipped)' } else { $ExternalRuntimeDir }
    external_code  = $ExternalCodeDir
    weights_policy = 'models/triposr 权重不入包；安装后 TRIPOSR_MODEL_DIR 指向 %LOCALAPPDATA%\TripoSR-Lite\models\triposr'
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $StagingDir 'staging-manifest.json') -Encoding utf8

Write-Host ''
Write-Host '[完成] staging 已组装（数据卷只打包 app/）' -ForegroundColor Green
