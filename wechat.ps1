param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CommandArgs)
$ErrorActionPreference = 'Stop'
$pilotNodeDir = Join-Path $PSScriptRoot '.runtime\node-v24.22.1-win-x64'
$pilotNode = Join-Path $pilotNodeDir 'node.exe'
$pilotCli = Join-Path $PSScriptRoot '.runtime\openclaw\node_modules\openclaw\openclaw.mjs'
$env:PATH = $pilotNodeDir + ';' + $env:PATH
$env:OPENCLAW_STATE_DIR = Join-Path $env:USERPROFILE '.openclaw-zhishi-pilot'
$env:OPENCLAW_CONFIG_PATH = Join-Path $env:OPENCLAW_STATE_DIR 'openclaw.json'
& $pilotNode $pilotCli @CommandArgs
exit $LASTEXITCODE
