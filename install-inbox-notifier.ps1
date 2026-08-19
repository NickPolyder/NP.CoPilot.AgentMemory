#requires -Version 7.0
<#
.SYNOPSIS
    Installs the np-agent-memory inbox notifier as a user-scoped Copilot CLI extension.

.DESCRIPTION
    Copies the bundled extension into ~/.copilot/extensions/ and records the
    plugin's absolute path in ~/.copilot/np-agent-memory/settings.json.
    Copilot CLI discovers extensions from the user extensions directory, not
    from an installed plugin's directory.
#>

[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$PluginRoot = (Split-Path -Parent $MyInvocation.MyCommand.Path),
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$resolvedPluginRoot = (Resolve-Path -LiteralPath $PluginRoot -ErrorAction Stop).Path
$sourceFile = Join-Path $resolvedPluginRoot '.github\extensions\np-agent-memory-inbox\extension.mjs'
if (-not (Test-Path -LiteralPath $sourceFile -PathType Leaf)) {
    throw "Inbox notifier source was not found: $sourceFile"
}

$copilotDirectory = Join-Path $HOME '.copilot'
$extensionDirectory = Join-Path $copilotDirectory 'extensions\np-agent-memory-inbox'
$targetFile = Join-Path $extensionDirectory 'extension.mjs'
$settingsDirectory = Join-Path $copilotDirectory 'np-agent-memory'
$settingsFile = Join-Path $settingsDirectory 'settings.json'

if (Test-Path -LiteralPath $targetFile) {
    $sourceHash = (Get-FileHash -LiteralPath $sourceFile).Hash
    $targetHash = (Get-FileHash -LiteralPath $targetFile).Hash
    if ($sourceHash -ne $targetHash -and -not $Force) {
        throw "Notifier extension already differs at '$targetFile'. Re-run with -Force to replace it."
    }
}

if ($PSCmdlet.ShouldProcess($targetFile, 'Install inbox notifier extension')) {
    New-Item -ItemType Directory -Path $extensionDirectory -Force | Out-Null
    Copy-Item -LiteralPath $sourceFile -Destination $targetFile -Force
    Write-Host "✅ Installed inbox notifier extension: $targetFile"
}

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
    Write-Host "✅ Recorded plugin root: $resolvedPluginRoot"
}

Write-Host 'Restart Copilot CLI, or run /clear, to load the notifier.'
