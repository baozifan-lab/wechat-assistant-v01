$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
# The application owns recovery for both saved WeChat accounts.
python app.py
