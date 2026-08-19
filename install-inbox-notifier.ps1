#requires -Version 7.0
<#
.SYNOPSIS
    Configures the bundled np-agent-memory inbox notifier.

.DESCRIPTION
    Records the installed plugin's absolute path in
    ~/.copilot/np-agent-memory/settings.json. The bundled extension reads this
    path when it launches the read-only inbox-summary command, avoiding any
    dependence on its loader path.
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

$settings['inboxNotifier']['pluginRoot'] = $resolvedPluginRoot

if ($PSCmdlet.ShouldProcess($settingsFile, 'Record inbox notifier plugin root')) {
    New-Item -ItemType Directory -Path $settingsDirectory -Force | Out-Null
    $settings |
        ConvertTo-Json -Depth 10 |
        Set-Content -LiteralPath $settingsFile -Encoding utf8NoBOM
    Write-Host "✅ Recorded bundled notifier root: $resolvedPluginRoot"
}

Write-Host 'Restart Copilot CLI, or run /clear, to load the updated notifier.'
