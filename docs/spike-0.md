# Spike 0 — Plugin Packaging Verification

**Status:** ✅ **GREEN LIGHT** — proceed to Phase 1, with one required `PLAN.md` amendment.

**Branch:** `chore/phase-0-spike`
**Date:** 2026-05-25
**Copilot CLI version:** `1.0.55-0`
**Python:** `3.14.5` (CPython, `cp314` wheels available)
**MCP SDK:** `mcp==1.26.0`

---

## 1. Goal

Verify, **before writing any production code**, that the Copilot CLI plugin
mechanism can:

1. Install a local Python plugin that ships a bundled venv.
2. Launch a stdio MCP server from that venv on agent session startup.
3. Expose the server's tools to the agent.
4. Discover a bundled skill.

…and characterize the runtime environment the server actually sees
(`cwd`, env vars, argv, process lifecycle) so the production design in
`docs/PLAN.md` is grounded in reality, not assumption.

## 2. Spike artifact

Everything lives under `spike/` at the repo root (deletable in one move
once Phase 1 starts). Layout:

```text
spike/
├── .claude-plugin/
│   ├── plugin.json                # name=np-agent-memory-spike, v0.0.1
│   └── marketplace.json           # marketplace name=np-agent-memory-spike-marketplace
├── .mcp.json                      # ONE stdio server; cwd OMITTED on purpose
├── install.ps1                    # idempotent venv builder
├── requirements.txt               # mcp==1.26.0
├── server/
│   └── spike_server/
│       ├── __init__.py
│       └── __main__.py            # FastMCP server, ONE tool: spike_ping
├── skills/
│   └── np-agent-memory-spike/
│       └── SKILL.md               # probe skill discovery
├── README.md
└── .gitignore                     # ignores .venv/
```

The one tool, `spike_ping(caller_cwd: str | None, note: str | None)`,
returns a rich snapshot of the server's runtime environment (see §4).

## 3. Install procedure that works (production-realistic path)

```powershell
# 1. Build the bundled venv (creates spike/.venv, pip-installs mcp==1.26.0).
cd C:\path\to\NP.CoPilot.AgentMemory\spike
.\install.ps1

# 2. Register the spike folder as a local marketplace.
copilot plugin marketplace add C:\path\to\NP.CoPilot.AgentMemory\spike

# 3. Install the plugin from that marketplace.
copilot plugin install np-agent-memory-spike@np-agent-memory-spike-marketplace
# -> "Installed 1 skill"

# 4. New Copilot CLI session — plugin is auto-loaded.
copilot -p "Call spike_ping with caller_cwd='C:\path\to\NP.CoPilot.AgentMemory'" --allow-all
```

The plugin (entire source folder, including `.venv/`) is copied to:

```text
C:\Users\<user>\.copilot\installed-plugins\np-agent-memory-spike-marketplace\np-agent-memory-spike\
```

`copilot plugin list` shows it as installed. The bundled skill
`np-agent-memory-spike` appears in the skill catalog from any new session.

> **Note:** `copilot mcp list` does **not** list plugin-source MCP servers
> (only user-config). This is a CLI subcommand quirk, not a packaging
> defect — plugin servers do load and are callable. Verify via `/env` or
> by actually calling a tool.

## 4. Observed runtime environment (one clean `spike_ping` call)

Cleaned-up JSON returned by the tool (Windows backslashes unescaped for
readability):

```json
{
  "server": {
    "cwd": "C:\\Users\\<user>\\.copilot\\installed-plugins\\np-agent-memory-spike-marketplace\\np-agent-memory-spike",
    "executable": "C:\\Users\\<user>\\.copilot\\installed-plugins\\np-agent-memory-spike-marketplace\\np-agent-memory-spike\\.venv\\Scripts\\python.exe",
    "sys_prefix": "C:\\Users\\<user>\\.copilot\\installed-plugins\\np-agent-memory-spike-marketplace\\np-agent-memory-spike\\.venv",
    "sys_base_prefix": "C:\\Users\\<user>\\AppData\\Local\\Programs\\Python\\Python314",
    "sys_version": "3.14.5 (tags/v3.14.5:5607950, May 10 2026, 10:43:50) [MSC v.1944 64 bit (AMD64)]",
    "argv": ["...\\server\\spike_server\\__main__.py"],
    "module_file": "...\\server\\spike_server\\__main__.py",
    "pid": 9796,
    "ppid": 15516,
    "platform": "Windows-11-10.0.26200-SP0",
    "started_at_iso": "2026-05-25T21:13:32.754528+00:00",
    "uptime_seconds": 13.317,
    "invocation_count": 1
  },
  "mcp": { "sdk": "mcp", "sdk_version": "1.26.0", "server_name": "np-agent-memory-spike" },
  "env": {
    "filtered": {
      "COPILOT_AGENT_SESSION_ID": "<session-id>",
      "COPILOT_CLI": "1",
      "COPILOT_CLI_BINARY_VERSION": "1.0.55-0",
      "COPILOT_CUSTOM_INSTRUCTIONS_DIRS": "C:\\Users\\<user>\\AppData\\Local\\agency\\logs\\session_<redacted>\\custom_instructions",
      "COPILOT_HOME": "C:\\Users\\<user>\\.copilot",
      "COPILOT_LOADER_PID": "35436",
      "COPILOT_RUN_APP": "1",
      "HOMEDRIVE": "C:",
      "HOMEPATH": "\\Users\\<user>",
      "PYTHONPATH": "C:\\Users\\<user>\\.copilot\\installed-plugins\\np-agent-memory-spike-marketplace\\np-agent-memory-spike/server",
      "PYTHONUNBUFFERED": "1",
      "SPIKE_PROBE_FROM_MCP_JSON": "hello-from-mcp-config",
      "USERPROFILE": "C:\\Users\\<user>",
      "WT_SESSION": "<wt-session-id>"
    }
  },
  "args": {
    "caller_cwd_echo": "C:\\path\\to\\NP.CoPilot.AgentMemory",
    "note_echo": "for-writeup"
  }
}
```

## 5. Install / run matrix

| Install mode | Server CWD | Python path | `PLUGIN_ROOT` substitution | `.mcp.json` env passed | Bundled skill discovered | Tool callable |
|---|---|---|---|---|---|---|
| `copilot plugin marketplace add` + `copilot plugin install` (persistent) | plugin install dir | `${PLUGIN_ROOT}/.venv/Scripts/python.exe` resolves | ✅ | ✅ (`SPIKE_PROBE_FROM_MCP_JSON`, `PYTHONPATH`, `PYTHONUNBUFFERED` all observed) | ✅ | ✅ |
| `copilot --plugin-dir <path>` (ephemeral) | *not exercised end-to-end this spike* — `mcp list --plugin-dir` did not enumerate the server (CLI subcommand limitation, same as above). Deferred as a Phase-1 follow-up. | — | — | — | — | — |

## 6. Findings

### ✅ Worked exactly as expected

- **`.claude-plugin/plugin.json` + `marketplace.json` + `.mcp.json`** is a
  valid local-plugin layout. No `mcpServers` block needed inside
  `plugin.json` when `.mcp.json` is present at the plugin root.
- **`${PLUGIN_ROOT}` substitution** in `command` and `env` values works on
  Windows with mixed separators
  (`${PLUGIN_ROOT}/.venv/Scripts/python.exe`).
- **`PYTHONPATH=${PLUGIN_ROOT}/server`** lets us run the server as
  `python -m spike_server` without a `pip install -e .` step.
- **Bundled venv is portable.** Copying the venv from the source folder
  to the install dir works fine — Python derives `sys.prefix` from the
  executable's parent, ignoring the stale `command =` path in
  `pyvenv.cfg`. `sys.base_prefix` correctly resolves to the system
  Python (`C:\Users\<user>\AppData\Local\Programs\Python\Python314`).
  All transitive native deps in `mcp==1.26.0` (pydantic-core, etc.)
  imported cleanly at the new path.
- **`.mcp.json` env values flow through** to the child process verbatim,
  including custom strings (`SPIKE_PROBE_FROM_MCP_JSON=hello-from-mcp-config`).
- **The Copilot CLI inherits its own env vars into the MCP child** — most
  notably `COPILOT_AGENT_SESSION_ID`, `COPILOT_LOADER_PID`, `COPILOT_HOME`,
  `COPILOT_CLI_BINARY_VERSION`, `WT_SESSION`.
- **Skill discovery works**: `np-agent-memory-spike` appears in the skill
  catalog of new CLI sessions (verified by querying skills with `copilot -p`).
- **Tool naming**: tools are auto-namespaced as
  `<plugin-name>-<tool-name>` (observed: `np-agent-memory-spike-spike_ping`).
  Important for avoiding collisions in Phase 1+.

### ⚠️ Gotchas / surprises

1. **Server CWD is NOT the agent's CWD.** `os.getcwd()` in the server
   returns the **plugin install dir** (because the plugin MCP loader
   injects `cwd = <install-dir>` when `.mcp.json` omits it; setting it
   explicitly would just change which non-agent dir we land in).
   ➡ **The agent identity model cannot derive the calling agent from
   `os.getcwd()`. Every tool that needs to scope to an agent must take
   an explicit `agent_cwd` parameter** (server canonicalizes it).
   This is the single required amendment to `PLAN.md` — see §8.
2. **`.gitignore` is not honored by `/plugin install`.** The entire
   source folder is copied verbatim, including `.venv/` (~50 MB).
   For the production plugin, the `install.ps1` step happens **after**
   the install copy, so the venv only lives in the install dir. The
   source repo does not need to commit a venv.
3. **Misconfigured stdio server fails silently to the agent.** With a
   bad `command` path (`python-does-not-exist.exe`), the agent simply
   sees "tool not found" — no error is surfaced to it. The CLI does
   log the failure: `[ERROR] Starting MCP client for <name> with command:
   …` and the spawned process's stderr appears in
   `~/.copilot/logs/process-<unix-ms>-<pid>.log` as
   `[ERROR] [mcp server <name> stderr] …`.
   ➡ **Production install.ps1 must self-verify** (e.g., run the bundled
   python once with `-c "import np_agent_memory; print('ok')"`) so a
   broken install is loud, not silent.
4. **`copilot mcp list` does not show plugin-source MCP servers** —
   only user-config. Verify plugin servers via `/env` in an interactive
   session, by calling a tool, or by tailing
   `~/.copilot/logs/process-*.log`.
5. **`COPILOT_CUSTOM_INSTRUCTIONS_DIRS`** points at a transient
   per-session temp dir
   (`…\agency\logs\session_<ts>_<pid>\custom_instructions`), not at the
   agent's CWD. So this env var is **not** a back-channel for agent
   identity — confirms the explicit `agent_cwd` parameter is the right
   path.
6. **Path-separator mixing on Windows is fine.** After `${PLUGIN_ROOT}`
   substitution, the observed `PYTHONPATH` is
   `C:\Users\<user>\.copilot\…\np-agent-memory-spike/server` (mixed
   `\` and `/`). Windows tolerates this; Python imports work.

### 🔁 Process lifecycle (matters for the SQLite design)

| Scenario | Observation |
|---|---|
| 3 sequential tool calls inside one `copilot -p` invocation | Same `pid` (e.g. 27516), `invocation_count` 1 → 2 → 3, increasing `uptime_seconds`. |
| Separate `copilot -p` invocation in the same terminal | New `pid` (e.g. 21980), `invocation_count` resets to 1, `uptime_seconds` ≈ 0–15s on first call. |
| Different terminal window / different CLI process | New `pid`, new `COPILOT_AGENT_SESSION_ID`, new `COPILOT_LOADER_PID`. |

**Conclusion:** One stdio MCP server process per Copilot CLI session
(one per terminal-window CLI invocation, roughly). Multiple terminal
windows → multiple concurrent server processes against the same SQLite
file. **This validates the multi-process design in `PLAN.md`** (WAL mode,
`BEGIN IMMEDIATE` for migrations, `busy_timeout=5000`, per-call
connections, no shared in-memory state).

### 📋 Observability

- Plugin loader logs to `~/.copilot/logs/process-<unix-ms>-<pid>.log`.
  This includes the full resolved command line, cwd, server lifecycle
  events, and **the server's stderr stream** prefixed
  `[mcp server <name> stderr]`. Good enough for production diagnostics.
  No need to ship our own log file in Phase 1.

### ❓ Not yet probed (acceptable follow-ups, not blockers)

- `copilot --plugin-dir <path>` end-to-end (interactive session,
  actually calling a tool). Useful for dev loop but not required for
  Phase 1 — marketplace install + uninstall + reinstall is workable.
- Whether MCP `roots` capability is supported by Copilot CLI as an
  alternative to per-call `agent_cwd` (would let the agent declare its
  workspace once per session). Worth a 30-minute investigation before
  Phase 3 hardens the tool surface; if unsupported, the explicit
  `agent_cwd` param stays.
- `/plugin reload` semantics in an interactive session (does it
  re-spawn MCP servers?).

## 7. The minimum-working `.mcp.json` (reference for Phase 1)

```json
{
  "mcpServers": {
    "np-agent-memory-spike": {
      "type": "local",
      "command": "${PLUGIN_ROOT}/.venv/Scripts/python.exe",
      "args": ["-m", "spike_server"],
      "env": {
        "PYTHONPATH": "${PLUGIN_ROOT}/server",
        "PYTHONUNBUFFERED": "1",
        "SPIKE_PROBE_FROM_MCP_JSON": "hello-from-mcp-config"
      }
    }
  }
}
```

`cwd` is intentionally omitted — the plugin loader injects
`cwd = <plugin install dir>` automatically. Setting it doesn't help us
(we can't make it the agent's CWD anyway), so leave it out for
simplicity.

## 8. Required `PLAN.md` amendment

The current plan describes "canonicalize the agent's path" as if the
server can derive it. **It cannot.** The amendment:

> All tools that need to scope to a calling agent (most notably
> `agent_register`, `note_add`, `handover_save`, and any read/query
> tools that take an implicit-agent context) MUST accept an explicit
> `agent_cwd: str` parameter. The server canonicalizes it (resolve →
> normalize case → strip trailing separators → forward-slashes on
> Windows for storage) and looks it up in `agent_aliases`.
>
> The bundled `np-agent-memory` skill must teach agents this contract:
> "Always pass your repository root as `agent_cwd`. The server will
> figure out which agent you are from that path." For most agents, this
> is `git rev-parse --show-toplevel` or the value of an environment
> variable they set at session start.
>
> Investigate MCP `roots` capability support in Copilot CLI before
> Phase 3. If supported, agents can declare their workspace roots once
> per session and we can derive `agent_cwd` server-side from
> `mcp.roots()`. If not, the explicit per-call parameter stays as the
> contract.

This is the **only** plan amendment required. Everything else in
`PLAN.md` is consistent with what the spike observed.

## 9. Verdict

✅ **GREEN LIGHT — proceed to Phase 1.**

- Stdio Python MCP plugin packaging works end-to-end.
- Bundled venv is portable across install-dir relocation.
- `${PLUGIN_ROOT}` substitution and env passthrough work as advertised.
- Multi-process model is real (one server per CLI session), validating
  the SQLite-with-WAL design.
- Observability via `~/.copilot/logs/process-*.log` is sufficient.
- One required `PLAN.md` amendment captured in §8.

Phase 1 can begin immediately after Nick reviews this writeup and
approves the `PLAN.md` amendment.

## 10. Cleanup checklist (for when Phase 1 starts)

- [ ] `copilot plugin uninstall np-agent-memory-spike@np-agent-memory-spike-marketplace`
- [ ] `copilot plugin marketplace remove np-agent-memory-spike-marketplace`
- [ ] `git rm -r spike/` on the Phase-1 branch
- [ ] Verify `~/.copilot/installed-plugins/np-agent-memory-spike-marketplace/` is gone
