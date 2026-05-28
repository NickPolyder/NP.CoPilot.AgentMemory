"""np-agent-memory MCP server package.

Phase 2: data folder provisioning + migration runner. The server creates
its runtime directory at $HOME/.copilot/np-agent-memory/ (or AGENT_MEMORY_DIR),
applies versioned SQL migrations on startup, and serves tools via MCP stdio.
"""

__version__ = "0.2.0"
