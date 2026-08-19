#requires -Version 7.0
<#
.SYNOPSIS
    Installs and configures the bundled np-agent-memory inbox notifier.

.DESCRIPTION
    Installs the plugin's console command into uv's persistent tool directory,
    then records the executable's absolute path in
    ~/.copilot/np-agent-memory/settings.json. The bundled extension invokes
    that executable directly for polling, avoiding per-poll uvx resolution.
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$PluginRoot = (Split-Path -Parent $MyInvocation.MyCommand.Path)
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$resolvedPluginRoot = (Resolve-Path -LiteralPath $PluginRoot -ErrorAction Stop).Path
$extensionFile = Join-Path $resolvedPluginRoot '.github\extensions\np-agent-memory-inbox\extension.mjs'
if (-not (Test-Path -LiteralPath $extensionFile -PathType Leaf)) {
    throw "Bundled inbox notifier was not found: $extensionFile"
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($null -eq $uv) {
    throw "uv was not found on PATH. Install uv, then re-run this script."
}

if ($PSCmdlet.ShouldProcess($resolvedPluginRoot, 'Install np-agent-memory as a uv tool')) {
    Write-Host '🛠  Installing np-agent-memory as a uv tool...'
    & $uv.Source tool install --from $resolvedPluginRoot --force np-agent-memory | Out-Host
    if ($LASTEXITCODE -ne 0) {
        throw "uv tool install failed (exit $LASTEXITCODE)."
    }
} else {
    return
}

$toolBinDirectory = (& $uv.Source tool dir --bin).Trim()
if ([string]::IsNullOrWhiteSpace($toolBinDirectory)) {
    throw 'uv did not return its tool executable directory.'
}

$executableName = if ($IsWindows) {
    'np-agent-memory.exe'
} else {
    'np-agent-memory'
}
$executablePath = Join-Path $toolBinDirectory $executableName
if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
    throw "uv installed the tool but its executable was not found: $executablePath"
}

$settingsDirectory = Join-Path $HOME '.copilot\np-agent-memory'
$settingsFile = Join-Path $settingsDirectory 'settings.json'
$settings = [ordered]@{}

if (Test-Path -LiteralPath $settingsFile) {
    try {
        $settings = Get-Content -LiteralPath $settingsFile -Raw |
            ConvertFrom-Json -AsHashtable -ErrorAction Stop
    } catch {
        throw "Cannot update invalid notifier settings at '$settingsFile': $($_.Exception.Message)"
    }
}

if ($null -eq $settings) {
    $settings = [ordered]@{}
}

if (
    -not $settings.ContainsKey('inboxNotifier') -or
    $settings['inboxNotifier'] -isnot [System.Collections.IDictionary]
) {
    $settings['inboxNotifier'] = [ordered]@{}
}

$settings['inboxNotifier']['executablePath'] = $executablePath

New-Item -ItemType Directory -Path $settingsDirectory -Force | Out-Null
$settings |
    ConvertTo-Json -Depth 10 |
    Set-Content -LiteralPath $settingsFile -Encoding utf8NoBOM
Write-Host "✅ Recorded inbox notifier executable: $executablePath"

Write-Host 'Restart Copilot CLI, or run /clear, to load the updated notifier.'
