$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pilotBinding = Join-Path $env:USERPROFILE '.openclaw-zhishi-pilot\zhishi-binding.json'
if (Test-Path -LiteralPath $pilotBinding) {
    Start-Process -FilePath (Get-Command python).Source -ArgumentList @('-X','utf8',(Join-Path $PSScriptRoot 'wechat_connect.py'),'--use-saved') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}
python app.py
