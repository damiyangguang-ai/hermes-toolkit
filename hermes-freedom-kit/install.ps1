# HFK 安装脚本（Windows）
#
# 做的事：
#   1. 找到一个带 ruamel.yaml 的 Python 解释器（优先 HFK 自己的 .venv）
#   2. 必要时创建 .venv 并安装 ruamel.yaml==0.18.17
#   3. 生成 hfk.cmd 启动器（已随仓库提供，此步只做校验）
#   4. 可选：把 Desktop 插件安装到 <HERMES_HOME>\desktop-plugins\hfk\
#
# 不做的事：不写注册表、不需要管理员权限、不修改 Hermes 仓库、不改动 config.yaml。
#
# 用法：
#   pwsh -File install.ps1                 # 核心 + 插件
#   pwsh -File install.ps1 -NoPlugin       # 只装核心
#   pwsh -File install.ps1 -SkipVenv       # 复用现有解释器

param(
    [switch]$NoPlugin,
    [switch]$SkipVenv
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($text) { Write-Host "[HFK] $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "[ OK ] $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "[WARN] $text" -ForegroundColor Yellow }

Write-Step "安装目录：$root"

# ---------------------------------------------------------------- 1. 解释器
$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$hermesPython = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
$candidates = @()
if (Test-Path $venvPython) { $candidates += $venvPython }
if (Test-Path $hermesPython) { $candidates += $hermesPython }

$usable = $null
foreach ($candidate in $candidates) {
    $probe = & $candidate -c "import ruamel.yaml,sys;print(ruamel.yaml.__version__)" 2>$null
    if ($LASTEXITCODE -eq 0 -and $probe) {
        Write-Ok "可用解释器：$candidate （ruamel.yaml $probe）"
        $usable = $candidate
        break
    }
}

if (-not $usable -and -not $SkipVenv) {
    Write-Step "创建虚拟环境 .venv"
    $systemPython = $null
    foreach ($name in @('py', 'python')) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { $systemPython = $cmd.Source; break }
    }
    if (-not $systemPython) {
        throw "找不到 Python。请先安装 Python 3.11+，或运行 install.ps1 -SkipVenv 复用已有解释器。"
    }
    & $systemPython -m venv (Join-Path $root '.venv')
    $pip = Join-Path $root '.venv\Scripts\pip.exe'
    Write-Step "安装依赖 ruamel.yaml==0.18.17（与 Hermes 0.23.1 同版本，保证写出字节一致）"
    & $pip install --quiet "ruamel.yaml==0.18.17"
    $usable = $venvPython
    Write-Ok "虚拟环境就绪"
}

if (-not $usable) {
    throw "没有可用的解释器，且指定了 -SkipVenv。"
}

# ---------------------------------------------------------------- 2. 自检
Write-Step "运行内置自检（不接触真实配置）"
$env:PYTHONPATH = $root
$env:PYTHONIOENCODING = 'utf-8'
& $usable -m hfk.cli selftest
if ($LASTEXITCODE -ne 0) { throw "自检失败，安装中止。请把上面的输出反馈给开发者。" }
Write-Ok "自检通过"

# ---------------------------------------------------------------- 3. 环境识别
Write-Step "识别 Hermes 环境"
$envJson = & $usable -m hfk.cli env --json 2>$null
$envExit = $LASTEXITCODE
if ($envExit -ne 0) {
    Write-Warn "未能识别 Hermes 环境。HFK 已安装，但在找到 Hermes 之前无法打补丁。"
} else {
    $info = $envJson | ConvertFrom-Json
    Write-Ok "Hermes $($info.version)（$($info.version_source)）／Home $($info.home)／Profile $($info.profile)"
    if (-not $info.config_readable) { Write-Warn "配置当前不可读：$($info.config_error)" }
}

# ---------------------------------------------------------------- 4. 插件
if (-not $NoPlugin) {
    $pluginSrc = Join-Path $root 'desktop-plugin\plugin.js'
    if (-not (Test-Path $pluginSrc)) {
        Write-Warn "未找到 desktop-plugin\plugin.js，跳过插件安装（核心功能不受影响）"
    } else {
        $hermesHome = if ($info -and $info.home) { $info.home } else { Join-Path $env:LOCALAPPDATA 'hermes' }
        $pluginDir = Join-Path $hermesHome 'desktop-plugins\hfk'
        New-Item -ItemType Directory -Force -Path $pluginDir | Out-Null
        Copy-Item -LiteralPath $pluginSrc -Destination (Join-Path $pluginDir 'plugin.js') -Force
        Write-Ok "插件已安装到 $pluginDir"
        Write-Host "        在 Hermes Desktop 中启用插件后，使用前需要先启动本地核心服务：hfk.cmd service" -ForegroundColor DarkGray
    }
}

Write-Host ""
Write-Ok "安装完成"
Write-Host "下一步：" -ForegroundColor Cyan
Write-Host "  hfk.cmd env                 # 看环境"
Write-Host "  hfk.cmd preview --preset balanced"
Write-Host "  hfk.cmd apply --preset balanced"
Write-Host "  hfk.cmd restore             # 随时还原"
