# 启动 DevFlow AI（面板 + 钉钉机器人 + 剪贴板监听 + 流水线）
Set-Location (Join-Path $PSScriptRoot "..")
& ".\.venv\Scripts\devflow.exe" serve @args
