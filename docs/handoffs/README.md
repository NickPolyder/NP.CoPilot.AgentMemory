# Cross-system handoff prompts (phases 9–12)

Phases 9–12 of [`../PLAN.md`](../PLAN.md) touch files **outside this repo**, so
the implementing agent (this repo) cannot do them directly. Each file here is a
self-contained prompt you can hand to the right agent — paste the whole file as
the task and it carries all the context that agent needs.

| Phase | Prompt | Target agent / owner | Depends on |
|-------|--------|----------------------|------------|
| 9  | [`phase-09-connects-ingest-rewrite.md`](phase-09-connects-ingest-rewrite.md) | **Connects agent** (`C:\path\to\Connects`) | Phase 5 ✅ |
| 10 | [`phase-10-handover-report-skill-rewrite.md`](phase-10-handover-report-skill-rewrite.md) | Whoever owns the **global skills** (`~/.copilot/skills/`) | Phase 5 ✅ |
| 11 | [`phase-11-workstream-instructions-update.md`](phase-11-workstream-instructions-update.md) | **User / machine config** (one global instruction) | Phase 8 ✅ |
| 12 | [`phase-12-handover-backfill.md`](phase-12-handover-backfill.md) | **Connects agent** (or ops) | Phase 10 |

All tools below are namespaced in the CLI as `np-agent-memory-<tool>` (e.g.
`np-agent-memory-handover_claim`). The plugin and its bundled
[`agent-memory` skill](../../skills/agent-memory/SKILL.md) are the reference for
tool semantics.
