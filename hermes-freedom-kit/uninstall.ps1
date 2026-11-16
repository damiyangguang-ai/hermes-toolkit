# HFK 一键卸载脚本（Windows）
#
# 顺序很重要：**先安全恢复配置，再删除程序文件**。
# 卸载绝不会：删除 Hermes、删除你的 config.yaml、删除 SOUL.md / Skills。
# 卸载一定会：保留所有原始备份与事务历史（供审计或手工回退）。
#
# 用法：
#   pwsh -File uninstall.ps1              # 完整卸载（保留备份数据）
#   pwsh -File uninstall.ps1 -KeepVenv    # 保留 Python 虚拟环境
#   pwsh -File uninstall.ps1 -KeepAll     # 只恢复配置，不删任何文件

param(
    [switch]$KeepVenv,
    [switch]$KeepAll
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

function Write-Step($text) { Write-Host "[HFK] $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "[ OK ] $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "[WARN] $text" -ForegroundColor Yellow }

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
$hermesPython = Join-Path $env:LOCALAPPDATA 'hermes\hermes-agent\venv\Scripts\python.exe'
$python = if (Test-Path $venvPython) { $venvPython } elseif (Test-Path $hermesPython) { $hermesPython } else { $null }

Write-Step "第 1 步：安全恢复配置"
$restored = $false
if (-not $python) {
    Write-Warn "找不到可用的 Python 解释器，无法自动恢复。请手动检查 config.yaml 中的 approvals.* 字段。"
} else {
    $env:PYTHONPATH = $root
    $env:PYTHONIOENCODING = 'utf-8'
    & $python -m hfk.cli uninstall
    if ($LASTEXITCODE -eq 0) {
        Write-Ok "配置已恢复；备份与事务历史已保留"
        $restored = $true
    } else {
        Write-Warn "自动恢复未成功。HFK 数据（含原始备份）仍然保留，可稍后重试。"
    }
}

if ($KeepAll) {
    Write-Host ""
    Write-Ok "已按要求只恢复配置，未删除任何文件"
    exit 0
}

Write-Step "第 2 步：清理 HFK 自身文件"
$candidates = @()
if (-not $KeepVenv) { $candidates += (Join-Path $root '.venv') }
$candidates += (Join-Path $root 'hfk.cmd')

foreach ($path in $candidates) {
    if (Test-Path $path) {
        Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path $path) { Write-Warn "未能删除：$path" } else { Write-Ok "已删除：$path" }
    }
}

Write-Host ""
Write-Ok "卸载完成"
if ($restored) {
    Write-Host "原始备份与事务历史保留在：" -ForegroundColor Cyan
    Write-Host "  <HERMES_HOME>\hfk\backups\" -ForegroundColor DarkGray
    Write-Host "  <HERMES_HOME>\hfk\transactions\transactions.json" -ForegroundColor DarkGray
}
Write-Host "Hermes 本体与你的配置未被删除。" -ForegroundColor DarkGray
