"""Regression coverage for the Copilot CLI's legacy MCP initialization."""

from __future__ import annotations

import os
import sys
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from np_agent_memory.__main__ import mcp


class TestLegacyStdioCompatibility:
    """The deployed server must accept the CLI's initialize handshake."""

    def test_registered_server_uses_legacy_sdk(self) -> None:
        """The v1 SDK is required until the CLI supports modern discovery."""
        assert pkg_version("mcp") == "1.26.0"
        assert mcp.name == "np-agent-memory"

    def test_stdio_server_initializes_and_serves_tools(
        self,
        tmp_path: Path,
    ) -> None:
        """A standard 2025-11-25 client can initialize, list, and call tools."""

        async def _run() -> None:
            source_root = Path(__file__).resolve().parents[1]
            data_dir = tmp_path / "agent-memory"
            python_path = [str(source_root)]
            if existing_python_path := os.environ.get("PYTHONPATH"):
                python_path.append(existing_python_path)

            server = StdioServerParameters(
                command=sys.executable,
                args=["-m", "np_agent_memory"],
                cwd=str(source_root.parent),
                env={
                    **os.environ,
                    "AGENT_MEMORY_DIR": str(data_dir),
                    "PYTHONPATH": os.pathsep.join(python_path),
                    "PYTHONUNBUFFERED": "1",
                },
            )

            with anyio.fail_after(30):
                async with stdio_client(server) as (read_stream, write_stream):
                    async with ClientSession(read_stream, write_stream) as session:
                        initialized = await session.initialize()
                        assert initialized.protocolVersion == "2025-11-25"

                        tools = await session.list_tools()
                        names = {tool.name for tool in tools.tools}
                        assert {"agent_register", "memory_alive", "memory_log"} <= names

                        result = await session.call_tool("memory_alive", {})
                        assert result.isError is False
                        content: dict[str, Any] = result.structuredContent or {}
                        assert content["server_name"] == "np-agent-memory"

        anyio.run(_run)
