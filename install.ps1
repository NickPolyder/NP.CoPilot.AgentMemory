#requires -Version 7.0
<#
.SYNOPSIS
    Development installer for the np-agent-memory Copilot CLI plugin.

.DESCRIPTION
    Creates (or reuses) a Python virtual environment at .venv\ and installs the
    project in editable mode with its dev extras (`pip install -e ".[dev]"`),
    then self-verifies by importing the server package via the venv's Python.

    Idempotent — safe to re-run.

    This is a DEVELOPMENT convenience (running tests, linting, self-verifying
    imports). At RUNTIME the plugin does NOT use this venv: .mcp.json launches
    `uvx --from ${PLUGIN_ROOT} np-agent-memory`, so uv builds and runs the
    server in its own managed environment (and can provision Python 3.12+
    itself). Consumers only need `uv` on PATH — see the README.

.NOTES
    Self-verification is a HARD requirement (docs/spike-0.md §6 gotcha #3):
    the Copilot CLI does not surface MCP-server start failures to agents,
    so a broken install must be loud at install time, not silent at runtime.
#>

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$pluginRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvDir    = Join-Path $pluginRoot '.venv'
$serverDir  = Join-Path $pluginRoot 'server'

Write-Host "📁 Plugin root:      $pluginRoot"
Write-Host "📁 Venv target:      $venvDir"
Write-Host "📁 Server source:    $serverDir"
Write-Host ''

# --- 1. Find a bootstrap Python interpreter (>= 3.12) ----------------------
# The migration runner depends on sqlite3.connect(autocommit=True) and
# datetime.UTC, both new in 3.12. We validate the interpreter version up front
# so we never create a venv we will only reject later.

function Test-PythonVersion {
    param([string[]]$PythonCommand)

    $exe = $PythonCommand[0]
    if (-not (Get-Command $exe -ErrorAction SilentlyContinue)) {
        return $null
    }
    $exeArgs = @($PythonCommand | Select-Object -Skip 1)
    $script = "import sys; print('%d.%d' % sys.version_info[:2]); sys.exit(0 if sys.version_info >= (3, 12) else 1)"
    $version = & $exe @($exeArgs + @('-c', $script)) 2>$null
    if ($LASTEXITCODE -eq 0) {
        return $version
    }
    return $null
}

# Probe candidates newest-first. `py -3` picks the newest installed 3.x; the
# explicit minor versions cover machines where the newest is < 3.12 but a
# supported runtime is still installed alongside it.
$bootstrapCandidates = @(
    @('py', '-3'),
    @('py', '-3.14'),
    @('py', '-3.13'),
    @('py', '-3.12'),
    @('python'),
    @('python3')
)

$pythonBootstrap = $null
$bootstrapVersion = $null
foreach ($candidate in $bootstrapCandidates) {
    $detected = Test-PythonVersion -PythonCommand $candidate
    if ($null -ne $detected) {
        $pythonBootstrap  = $candidate
        $bootstrapVersion = $detected
        break
    }
}

if ($null -eq $pythonBootstrap) {
    throw "No Python 3.12+ interpreter found. Install Python 3.12+ and re-run."
}
Write-Host "🐍 Bootstrap Python: $($pythonBootstrap -join ' ') (v$bootstrapVersion)"

$venvPython = Join-Path $venvDir 'Scripts\python.exe'

# --- 2. Create, reuse, or rebuild the venv ---------------------------------
# A venv created by an older first run (e.g. on 3.11) installs cleanly but
# crashes at first DB init. Detect that here and rebuild from the validated
# bootstrap rather than reusing a venv we cannot support (review R5).

function New-Venv {
    param([string[]]$PythonCommand, [string]$TargetDir)

    Write-Host "🛠  Creating venv..."
    $exe = $PythonCommand[0]
    $exeArgs = @($PythonCommand | Select-Object -Skip 1)
    & $exe @($exeArgs + @('-m', 'venv', $TargetDir))
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed (exit $LASTEXITCODE)" }
}

$needsCreate = $true
if (Test-Path -LiteralPath $venvPython) {
    $existingVersion = Test-PythonVersion -PythonCommand @($venvPython)
    if ($null -ne $existingVersion) {
        Write-Host "✅ Venv already exists (Python $existingVersion) — reusing."
        $needsCreate = $false
    } else {
        Write-Host "♻  Existing venv is unsupported (Python < 3.12) — rebuilding."
        Remove-Item -LiteralPath $venvDir -Recurse -Force
    }
}

if ($needsCreate) {
    New-Venv -PythonCommand $pythonBootstrap -TargetDir $venvDir
}

if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Expected venv Python at '$venvPython' but it does not exist."
}

# --- 2b. Re-confirm the venv interpreter is 3.12+ --------------------------

Write-Host "🔎 Verifying Python version (>= 3.12)..."
$versionOutput = Test-PythonVersion -PythonCommand @($venvPython)
if ($null -eq $versionOutput) {
    throw "Python 3.12+ is required, but the venv interpreter is not. Delete '$venvDir' and re-run."
}
Write-Host "✅ Python $versionOutput OK."

# --- 3. Install the project (editable) + dev extras ------------------------

Write-Host "🛠  Upgrading pip in venv..."
& $venvPython -m pip install --upgrade --disable-pip-version-check pip | Out-Host
if ($LASTEXITCODE -ne 0) { throw "pip upgrade failed (exit $LASTEXITCODE)" }

Write-Host "🛠  Installing project (editable) with dev extras from pyproject.toml..."
# --no-cache-dir: we observed corrupt cached wheels on Python 3.14 / pip 26.1.1
# producing site-packages without the .pyd binaries (pywin32, rpds-py,
# pydantic-core, etc.), which made the server unimportable. Re-downloading
# fresh wheels every install costs a few seconds but is always correct.
& $venvPython -m pip install --disable-pip-version-check --no-cache-dir -e "$pluginRoot[dev]" | Out-Host
if ($LASTEXITCODE -ne 0) { throw "editable install failed (exit $LASTEXITCODE)" }

# --- 4. Self-verify --------------------------------------------------------
# Importing np_agent_memory.__main__ exercises:
#   * the venv's site-packages (mcp SDK importable)
#   * the editable install (np_agent_memory resolves to server/np_agent_memory)
#   * FastMCP instantiation at module load (the `mcp = FastMCP(...)` line
#     runs but `mcp.run()` does NOT, because __name__ != "__main__")
# A failure here means the production plugin would also fail to start
# silently — which is exactly what we are guarding against.

Write-Host "🔎 Self-verifying server package import..."

$selfCheckScript = @"
import json
import np_agent_memory.__main__ as m

print(json.dumps({
    "package_version": m.PACKAGE_VERSION,
    "mcp_sdk_version": m._MCP_SDK_VERSION,
    "server_name": m.mcp.name,
}))
"@

$selfCheckOutput = & $venvPython -c $selfCheckScript 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "self-verify failed (exit $LASTEXITCODE)`n$selfCheckOutput"
}

$lastLine = ($selfCheckOutput | Select-Object -Last 1)
try {
    $parsed = $lastLine | ConvertFrom-Json -ErrorAction Stop
    Write-Host "✅ Self-verify OK:"
    Write-Host "   server_name:     $($parsed.server_name)"
    Write-Host "   package_version: $($parsed.package_version)"
    Write-Host "   mcp_sdk_version: $($parsed.mcp_sdk_version)"
} catch {
    throw "self-verify produced unexpected output (could not parse last line as JSON):`n$selfCheckOutput"
}

# --- 5. Next steps ---------------------------------------------------------

Write-Host ''
Write-Host "✅ Dev install complete."
Write-Host "   Venv Python: $venvPython"
Write-Host ''
Write-Host "Run the tests:"
Write-Host "  `$env:PYTHONPATH = `"$serverDir`"; & `"$venvPython`" -m pytest server\tests -q"
Write-Host ''
Write-Host "Install the plugin (consumers only need 'uv' on PATH):"
Write-Host "  1) Restart Copilot CLI."
Write-Host "  2) /plugin marketplace add `"$pluginRoot`""
Write-Host "  3) /plugin install np-agent-memory@np-agent-memory-marketplace"
Write-Host "  4) Restart again, then call the np-agent-memory-memory_alive tool to confirm."
