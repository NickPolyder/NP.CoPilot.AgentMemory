# Inbox notifier registration repair

**Date:** 2026-08-19

## Problem

Version 0.9.1 declared the inbox notifier as a bundled plugin extension.
The notifier inferred the plugin root by walking parent directories from
its loader path.
That path can be malformed by plugin loading, producing an invalid `uvx --from`
argument and a dependency-resolution attempt on every polling interval.

## Resolution

- Keep the bundled `extensions` declaration in `.claude-plugin/plugin.json`.
- Install the plugin console command once with `install-inbox-notifier.ps1`
  and require the extension to use its absolute executable path.

The notifier now disables itself with one actionable warning when its
executable is not configured, rather than resolving packages with `uvx` every
interval.
