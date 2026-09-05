# DevFlow AI 一键安装（Windows PowerShell）
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".venv")) {
    Write-Host "创建虚拟环境 .venv ..."
    python -m venv .venv
}
$py = ".\.venv\Scripts\python.exe"
& $py -m pip install --upgrade pip
& $py -m pip install -e .
Write-Host "安装 Playwright Chromium（用于 AI 截图测试）..."
& $py -m playwright install chromium
& ".\.venv\Scripts\devflow.exe" init
Write-Host ""
Write-Host "安装完成。编辑 config.yaml 后运行 scripts\start.ps1 或 .venv\Scripts\devflow.exe serve" -ForegroundColor Green
