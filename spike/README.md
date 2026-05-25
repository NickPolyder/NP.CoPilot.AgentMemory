# spike/ — Phase 0 plugin packaging spike

**Throwaway.** This folder will be deleted in one move once Phase 0 is signed
off and Phase 1 scaffolding begins. Don't import from it, don't depend on it.

## What it does
- Ships a one-tool stdio MCP server (`spike_ping`) packaged as a Copilot CLI
  plugin (`np-agent-memory-spike`).
- Verifies the Copilot CLI plugin mechanism actually launches the server,
  passes through env vars, and discovers a bundled skill.
- Probes the runtime environment the server sees (cwd, argv, env, pid, venv
  layout) so Phase 1 can be designed against reality, not assumptions.

## Install
```powershell
pwsh .\install.ps1
copilot   # restart any existing CLI windows
# inside Copilot CLI:
/plugin marketplace add C:\path\to\NP.CoPilot.AgentMemory\spike
/plugin install np-agent-memory-spike@np-agent-memory-spike-marketplace
```

## Dev loop (no install)
```powershell
copilot --plugin-dir C:\path\to\NP.CoPilot.AgentMemory\spike
```

## Write-up
See [`../docs/spike-0.md`](../docs/spike-0.md).
