"""MCP tool registration.

Each domain (agents, memory, todos, …) lives in its own module exposing a
``register_*_tools(mcp)`` function. ``__main__`` calls them once at import so
the tool surface stays declarative and the entry point stays thin.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from np_agent_memory.backup import register_backup_tools
from np_agent_memory.tools.agents import register_agent_tools
from np_agent_memory.tools.blockers import register_blocker_tools
from np_agent_memory.tools.handovers import register_handover_tools
from np_agent_memory.tools.inbox import register_inbox_tools
from np_agent_memory.tools.memory import register_memory_tools
from np_agent_memory.tools.todos import register_todo_tools


def register_all_tools(mcp: FastMCP) -> None:
    """Register every tool module onto the FastMCP server."""
    register_agent_tools(mcp)
    register_memory_tools(mcp)
    register_todo_tools(mcp)
    register_blocker_tools(mcp)
    register_handover_tools(mcp)
    register_inbox_tools(mcp)
    register_backup_tools(mcp)


__all__ = [
    "register_agent_tools",
    "register_all_tools",
    "register_backup_tools",
    "register_blocker_tools",
    "register_handover_tools",
    "register_inbox_tools",
    "register_memory_tools",
    "register_todo_tools",
]
