#Requires -Version 5.1
<#
.SYNOPSIS
    Run the Global Cinema Opportunity Radar CLI from the project virtualenv.

.DESCRIPTION
    Wrapper for Windows so you do not have to activate the venv or remember the
    python path. Sets RADAR_HOME to the repository root and forwards every
    argument and the exit code.

.EXAMPLES
    .\scripts\run-radar.ps1 doctor
    .\scripts\run-radar.ps1 run --force --digest
    .\scripts\run-radar.ps1 opportunities --min-score 60 --limit 20
    .\scripts\run-radar.ps1 serve
#>
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $RadarArgs
)

$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    Write-Error "Virtualenv not found at $python. Run: python -m venv .venv; .venv\Scripts\pip install -r requirements.txt"
    exit 1
}

$env:RADAR_HOME = $root
# Keep any existing PYTHONPATH; the repository root just has to be on it.
if ($env:PYTHONPATH -and $env:PYTHONPATH -notlike "*$root*") {
    $env:PYTHONPATH = "$root;$env:PYTHONPATH"
} elseif (-not $env:PYTHONPATH) {
    $env:PYTHONPATH = $root
}

Push-Location $root
try {
    & $python -m radar @RadarArgs
    exit $LASTEXITCODE
}
finally {
    Pop-Location
}
