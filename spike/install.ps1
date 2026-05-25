#requires -Version 7.0
<#
.SYNOPSIS
    Phase 0 spike installer for np-agent-memory-spike.

.DESCRIPTION
    Creates a Python virtual environment at spike\.venv and pip-installs the
    pinned dependencies from requirements.txt. Idempotent — safe to re-run.

    The venv lives INSIDE the plugin folder so that the Copilot CLI's
    /plugin install command will copy it as part of the plugin package
    (one of the things this spike validates).

.NOTES
    Pin the absolute interpreter path in .mcp.json via the ${PLUGIN_ROOT}
    placeholder, so the plugin does not depend on whatever "python" happens
    to be on PATH when the CLI launches it.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$spikeRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir   = Join-Path $spikeRoot '.venv'
$reqFile   = Join-Path $spikeRoot 'requirements.txt'

Write-Host "📁 Spike root:       $spikeRoot"
Write-Host "📁 Venv target:      $venvDir"
Write-Host "📄 Requirements:     $reqFile"
Write-Host ''

# Find a Python interpreter. Prefer `py -3` (Windows launcher) for stability.
$pythonBootstrap = $null
if (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonBootstrap = @('py', '-3')
    Write-Host "🐍 Bootstrap Python: py -3"
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonBootstrap = @('python')
    Write-Host "🐍 Bootstrap Python: python (from PATH)"
} else {
    throw "No Python interpreter found. Install Python 3.10+ and re-run."
}

$venvPython = Join-Path $venvDir 'Scripts\python.exe'

if (Test-Path -LiteralPath $venvPython) {
    Write-Host "✅ Venv already exists — reusing."
} else {
    Write-Host "🛠  Creating venv..."
    & $pythonBootstrap[0] @($pythonBootstrap[1..($pythonBootstrap.Length - 1)] + @('-m', 'venv', $venvDir))
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Expected venv Python at '$venvPython' but it does not exist."
}

Write-Host "🛠  Upgrading pip in venv..."
& $venvPython -m pip install --upgrade --disable-pip-version-check pip | Out-Host
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (exit $LASTEXITCODE)" }

Write-Host "🛠  Installing pinned dependencies..."
& $venvPython -m pip install --disable-pip-version-check -r $reqFile | Out-Host
if ($LASTEXITCODE -ne 0) { throw "pip install failed (exit $LASTEXITCODE)" }

Write-Host ''
Write-Host "✅ Install complete."
Write-Host "   Venv Python: $venvPython"
Write-Host ''
Write-Host "Next steps:"
Write-Host "  1) Restart Copilot CLI."
Write-Host "  2) /plugin marketplace add `"$spikeRoot`""
Write-Host "  3) /plugin install np-agent-memory-spike@np-agent-memory-spike-marketplace"
Write-Host "  4) Restart again, then call the spike_ping tool."
