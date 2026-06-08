# ADR 0002 — Cross-agent addressing: a stable public handle

- **Status:** Accepted
- **Date:** 2026-06-08
- **Decider:** Nick Polyderopoulos
- **Context window:** Phase 3 closeout, pre-Phase-4
- **Related:** [`docs/PLAN.md`](../PLAN.md), [`docs/TASKS.md`](../TASKS.md),
  [`docs/phase-3-next-steps.md`](../phase-3-next-steps.md),
  [`server/np_agent_memory/migrations/0001_init.sql`](../../server/np_agent_memory/migrations/0001_init.sql)

## Context

The Phase 3 rubber-duck pass flagged a design gap that the identity model
leaves unresolved:

> Agent `name` is **not unique** in the schema (`agents.name text not null`,
> no constraint). Agents never see internal ULIDs (a hard project rule), so
> the **only** stable public identifier an agent currently has is its
> canonical path. Any tool that addresses *another* agent — most immediately
> `inbox_send` (Phase 6), and any future cross-agent coordination — therefore
> has no unambiguous, human-friendly handle to address.

Three facts from the current design frame the decision:

1. **ULIDs are internal-only.** `agents.id` is the FK target everywhere, but
   tool responses return name/path only. We will not expose it as the public
   handle.
2. **`name` is a mutable display label.** `register_agent` rewrites `name` on
   *every* session-start call (`agents.py:54`). It is a label like
   `"backend-developer"`, deliberately not unique — two repos can each have a
   `"backend-developer"` agent.
3. **Canonical path is already a unique key.** `agent_aliases.alias_path` is
   the PRIMARY KEY and is how every agent-scoped tool resolves the caller. It
   is unique, but it is a machine-specific absolute filesystem path.

The decided inbox behaviour (`docs/TASKS.md`) is to **"accept both canonical
path and registered name"** for addressing — but "registered name" is
ambiguous today. This ADR resolves what the stable, unambiguous handle is.

## Options considered

### Option A — Add a public `agent_key` slug column (chosen)

Add an immutable, unique, human-friendly `agent_key` (e.g. `backend-developer`,
`backend-developer-2`) to `agents`. Minted once at first registration,
returned by `agent_register` / `agent_describe`, and accepted by every
addressing tool.

- **Pros**
  - Decouples the **stable identity handle** from the **mutable display
    name**. Renaming `name` never breaks addressing.
  - Human-friendly and shareable — "send the handover to `analytics-agent`"
    — without leaking absolute filesystem paths.
  - Uniqueness enforced at the database, so addressing is unambiguous by
    construction.
  - Honours the "agents never see ULIDs" rule while still giving a stable
    public identifier.
  - Set-once semantics leave the idempotent `register_agent` upsert intact —
    routine session-start re-registration never collides.
- **Cons**
  - One new concept agents must learn (the bundled skill, Phase 8, teaches
    it).
  - Requires a new migration (`0002_*.sql`) and a small amount of slug /
    collision logic in `register_agent`.
  - The sender must know the recipient's `agent_key` (true of any handle
    short of broadcast).

### Option B — Enforce `UNIQUE(name)` (or `UNIQUE(workstream, name)`) and address by name

Make the existing `name` the addressing key by constraining it.

- **Pros**
  - No new column or concept; address by the name you already have.
- **Cons**
  - **Overloads a mutable display field with identity semantics.** `name` is
    rewritten on every register; a unique constraint turns a routine
    session-start `register_agent` into something that can *fail* on
    collision. That is a sharp edge on the hottest path.
  - Legitimately-shared names break: two `"backend-developer"` agents in
    different repos can no longer coexist.
  - `UNIQUE(workstream, name)` does not fix it: `workstream` is nullable, and
    SQLite treats `NULL`s as distinct, so unregistered-workstream agents still
    collide-or-don't unpredictably.
  - Renames acquire failure modes mid-session. Identity should not be a field
    the user edits for display.

### Option C — Address strictly by canonical path (no name addressing)

Use the already-unique `alias_path` as the only addressing key.

- **Pros**
  - Zero schema change; the key already exists and is already how tools
    resolve agents.
- **Cons**
  - The sender must know the recipient's **absolute canonical path** — brittle,
    machine-specific, and it leaks filesystem layout into messages.
  - Hostile UX for the actual use case ("message the analytics-agent"): you
    rarely know another agent's repo root, and it changes across machines.
  - Contradicts the decided inbox behaviour of accepting a "registered name".

## Decision

**Adopt Option A — add an immutable, unique `agent_key` public handle.**

Addressing tools accept **either** an `agent_key` **or** a canonical path
(satisfying the "accept both" requirement in `docs/TASKS.md`), and resolve
both to the internal ULID server-side. `name` remains a free-form, mutable
display label with no uniqueness.

### Implementation direction (for the consuming phase)

The schema change lands in a **new** migration `0002_add_agent_key.sql` — never
edit `0001_init.sql` (checksum guard). To keep the migration pure-SQL and avoid
slugifying in SQLite, use a **nullable column + partial unique index** and mint
the key in the application layer:

```sql
-- 0002_add_agent_key.sql
alter table agents add column agent_key text;

create unique index if not exists idx_agents_agent_key
  on agents(agent_key)
  where agent_key is not null;
```

`register_agent` owns key assignment:

- Accept an **optional explicit** `agent_key` argument; otherwise derive a slug
  from `name` (lowercase, non-alphanumerics → hyphen, collapse/trim hyphens).
- Resolve collisions with a numeric suffix (`-2`, `-3`, …) inside the same
  `BEGIN IMMEDIATE` transaction so concurrent first-registrations stay
  consistent.
- **Set once.** Assign only when the row's `agent_key` is `null`; never rewrite
  it on subsequent registers. Validate an explicitly-supplied key against the
  slug format and reject duplicates.
- Lazily backfill: any pre-existing row (a handful of throwaway Phase 3 test
  agents at most) gets a key on its next `register_agent` call. No SQL-side
  backfill needed.

`agent_register` and `agent_describe` add `agent_key` to their return payloads
so the bundled skill (Phase 8) can teach each agent its own handle.

**Sequencing:** the migration + `register_agent` changes should land with the
first consumer that needs the handle — that is **Phase 6 (inbox)**, optionally
pulled earlier into Phase 4 if the bundled-skill/identity surface wants the key
sooner. This ADR settles the *design*; it does not itself add the column.

## Consequences

### Positive

- Cross-agent addressing (`inbox_send`, future coordination) has an
  unambiguous, stable, human-friendly key by construction.
- Display name stays freely editable; identity and label are cleanly
  separated.
- No internal ULID is ever exposed.
- "Accept both path and name" from `docs/TASKS.md` is satisfiable precisely.

### Negative

- One additional migration and a modest amount of slug/collision logic in
  `register_agent`.
- One more field for agents (and the bundled skill) to understand.

### Neutral / forward-looking

- The partial-unique-index + lazy-mint approach keeps the migration trivial
  and pure-SQL, consistent with the atomic-migration runner constraints
  (no SQL-side string munging, no data backfill step).
- A future global directory/dashboard (out of scope for v1) can key off
  `agent_key` as the public identifier.

## References

- [`docs/phase-3-next-steps.md`](../phase-3-next-steps.md) §2 — the gap this
  ADR closes
- [`server/np_agent_memory/tools/agents.py`](../../server/np_agent_memory/tools/agents.py)
  — `register_agent` (the mint point) and the no-ULID response contract
- [`server/np_agent_memory/migrations/0001_init.sql`](../../server/np_agent_memory/migrations/0001_init.sql)
  — current `agents` / `inbox` schema
- [`docs/decisions/0001-stdio-vs-long-lived-backend.md`](0001-stdio-vs-long-lived-backend.md)
  — ADR format precedent
