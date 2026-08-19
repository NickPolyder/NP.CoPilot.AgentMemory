# Inbox notifier registration repair

**Date:** 2026-08-19

## Problem

Version 0.9.1 declared the inbox notifier as an installed-plugin extension.
Copilot CLI discovers extensions from project and user extension directories,
not from plugin manifests.
The notifier also inferred the plugin root by walking parent directories from
its loader path.
That path can be malformed by plugin loading, producing an invalid `uvx --from`
argument and a dependency-resolution attempt on every polling interval.

## Resolution

- Remove the unsupported `extensions` declaration from `.claude-plugin/plugin.json`.
- Install the notifier explicitly into `$HOME\.copilot\extensions\` with
  `install-inbox-notifier.ps1`.
- Persist the installed plugin root in the notifier settings and require the
  extension to use that absolute path.

The notifier now disables itself with one actionable warning when its root is
not configured, rather than retrying a broken `uvx` command every interval.
