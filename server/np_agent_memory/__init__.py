"""np-agent-memory MCP server package.

Phase 3: agent identity layer on top of the Phase 2 data folder + migrations.
The server resolves each caller to an agent via the canonicalized ``agent_cwd``
they pass, and exposes ``agent_register`` / ``agent_describe`` /
``agent_add_alias``.
"""

__version__ = "0.3.0"
