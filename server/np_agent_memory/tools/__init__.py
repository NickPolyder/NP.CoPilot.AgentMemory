"""MCP tool registration.

Each domain (agents, memory, todos, …) lives in its own module exposing a
``register_*_tools(mcp)`` function. ``__main__`` calls them once at import so
the tool surface stays declarative and the entry point stays thin.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from np_agent_memory.tools.agents import register_agent_tools


def register_all_tools(mcp: FastMCP) -> None:
    """Register every tool module onto the FastMCP server."""
    register_agent_tools(mcp)


__all__ = ["register_agent_tools", "register_all_tools"]
