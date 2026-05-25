---
name: np-agent-memory-spike
description: Phase 0 spike skill. Used only to verify that a skill bundled inside a Copilot CLI plugin is discovered by /skills after install. NOT for production use — will be deleted when Phase 1 begins.
---

# np-agent-memory-spike skill

This skill exists purely to validate **skill discovery** for the Phase 0 spike
of the np-agent-memory plugin.

If you can see this file via `/skills`, then bundling a skill at
`skills/<plugin-name>/SKILL.md` inside a Copilot CLI plugin works as expected.

## What to do if invoked

Call the `spike_ping` MCP tool (from the `np-agent-memory-spike` server) and
report its full output. Nothing else.

## When this skill goes away

This entire `spike/` folder is deleted once Phase 0 is signed off and Phase 1
scaffolding begins. The real plugin will ship a different, production-grade
skill under `skills/agent-memory/SKILL.md`.
