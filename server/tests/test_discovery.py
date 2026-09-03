"""Regression tests for MCP SDK v2 protocol discovery compatibility.

See ``docs/bugs/mcp-server-discover-compatibility.md`` for the incident this
guards against: an MCP SDK v1 server rejects the ``server/discover`` request
that a v2 ``Client`` (in ``mode="auto"``, the default) sends while probing a
new connection under the 2026-07-28 protocol revision. These tests exercise
the *real* registered server (``np_agent_memory.__main__.mcp``) end-to-end
over the official in-memory ``Client`` transport — no subprocess, no
transport layer, matching the SDK's own recommended testing pattern — to
prove:

1. A modern client completes discovery and negotiates the 2026-07-28
   protocol revision (the exact path that used to fail).
2. A legacy client (2025-11-25 ``initialize`` handshake, no discovery) is
   still served correctly, so existing hosts are not broken by the upgrade.
3. Tools are listed and callable over the discovery-negotiated session.
4. The deployed stdio server answers discovery directly, without falling back
   to the legacy handshake.

Note on why the ``discover_result`` / ``initialize_result`` assertions below
exist: the SDK's ``negotiate_auto`` (``mcp/client/_probe.py``) catches an
``MCPError`` from ``server/discover`` and **silently falls back** to
``initialize()``. A connection therefore still succeeds against a server
that rejects discovery, so asserting only "the client connected" would not
reproduce the defect. Exactly one of ``discover_result`` /
``initialize_result`` is populated per connection, which makes them a direct,
constant-independent witness of *which* negotiation path the server drove.
"""

from __future__ import annotations

import os
import sys
import tomllib
from importlib.metadata import version as pkg_version
from pathlib import Path
from typing import Any

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.types.version import LATEST_MODERN_VERSION
from np_agent_memory.__main__ import mcp


class TestDiscoverCompatibility:
    """Regression coverage for the v1 -> v2 ``server/discover`` migration."""

    def test_default_client_negotiates_modern_discover_protocol(self) -> None:
        """A default (``mode="auto"``) client must reach the 2026-07-28 wire.

        This is the exact connection path a current Copilot host uses. Before
        the migration, the server ran MCP SDK v1, which has no handler for
        ``server/discover`` and rejected the request outright.
        """

        async def _run() -> None:
            async with Client(mcp, raise_exceptions=True) as client:
                assert client.protocol_version == LATEST_MODERN_VERSION
                assert client.server_info is not None
                assert client.server_info.name == "np-agent-memory"

        anyio.run(_run)

    def test_auto_mode_client_gets_discover_probe_answered(self) -> None:
        """The server must actually answer ``server/discover``.

        ``discover_result`` is populated only when the probe is answered, so
        this asserts the mechanism directly rather than inferring it from a
        negotiated version constant.
        """

        async def _run() -> None:
            async with Client(mcp, raise_exceptions=True) as client:
                assert client.session.discover_result is not None

        anyio.run(_run)

    def test_auto_mode_client_does_not_fall_back_to_initialize(self) -> None:
        """Auto-mode must not silently degrade to the legacy handshake.

        Kills the mutation where the server rejects ``server/discover``: the
        SDK swallows that error and calls ``initialize()``, so the connection
        still succeeds. A populated ``initialize_result`` is the fingerprint
        of that fallback, and it must be absent.
        """

        async def _run() -> None:
            async with Client(mcp, raise_exceptions=True) as client:
                assert client.session.initialize_result is None

        anyio.run(_run)

    def test_tools_are_listed_and_callable_over_discover_negotiated_session(
        self,
    ) -> None:
        """Tools must be listable and callable once discovery has negotiated
        the session — discovery succeeding is not enough on its own if the
        tool surface behind it is unreachable.
        """

        async def _run() -> None:
            async with Client(mcp, raise_exceptions=True) as client:
                assert client.protocol_version == LATEST_MODERN_VERSION

                tools = await client.list_tools()
                names = {t.name for t in tools.tools}
                assert {"agent_register", "memory_log", "memory_alive"} <= names

                result = await client.call_tool("memory_alive", {})
                assert result.is_error is False
                content: dict[str, Any] = result.structured_content or {}
                assert content["server_name"] == "np-agent-memory"
                assert content["mcp_sdk_version"] == pkg_version("mcp")

        anyio.run(_run)

    def test_stdio_server_answers_discover_without_initialize_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        """The deployed stdio transport must answer discovery directly."""

        async def _run() -> None:
            source_root = Path(__file__).resolve().parents[1]
            data_dir = tmp_path / "agent-memory"
            data_dir.mkdir()
            python_path = [str(source_root)]
            if existing_python_path := os.environ.get("PYTHONPATH"):
                python_path.append(existing_python_path)

            with anyio.fail_after(30):
                async with Client(
                    StdioServerParameters(
                        command=sys.executable,
                        args=["-m", "np_agent_memory"],
                        cwd=source_root.parent,
                        env={
                            **os.environ,
                            "AGENT_MEMORY_DIR": str(data_dir),
                            "PYTHONPATH": os.pathsep.join(python_path),
                            "PYTHONUNBUFFERED": "1",
                        },
                    ),
                    raise_exceptions=True,
                ) as client:
                    assert client.protocol_version == LATEST_MODERN_VERSION
                    assert client.session.discover_result is not None
                    assert client.session.initialize_result is None

                    result = await client.call_tool("memory_alive", {})
                    assert result.is_error is False

        anyio.run(_run)


class TestLegacyClientCompatibility:
    """The v2 upgrade must not strand hosts pinned to the older handshake."""

    def test_legacy_client_still_completes_initialize_handshake(self) -> None:
        """A legacy client pinned to the pre-2026-07-28 handshake must still
        connect, so the migration does not strand hosts that only speak the
        older ``initialize`` handshake.
        """

        async def _run() -> None:
            async with Client(mcp, raise_exceptions=True, mode="legacy") as client:
                assert client.protocol_version == "2025-11-25"

        anyio.run(_run)

    def test_legacy_client_takes_handshake_path_not_discovery(self) -> None:
        """A legacy connection must negotiate without any discovery probe."""

        async def _run() -> None:
            async with Client(mcp, raise_exceptions=True, mode="legacy") as client:
                assert client.session.discover_result is None

        anyio.run(_run)

    def test_legacy_client_can_list_tools(self) -> None:
        """Completing the handshake is not enough: the tool surface behind a
        legacy session must remain reachable.
        """

        async def _run() -> None:
            async with Client(mcp, raise_exceptions=True, mode="legacy") as client:
                tools = await client.list_tools()

                assert {"agent_register", "memory_log", "memory_alive"} <= {
                    t.name for t in tools.tools
                }

        anyio.run(_run)

    def test_legacy_client_can_call_tool(self) -> None:
        """A legacy session must still be able to execute a tool, not merely
        enumerate one.
        """

        async def _run() -> None:
            async with Client(mcp, raise_exceptions=True, mode="legacy") as client:
                result = await client.call_tool("memory_alive", {})

                assert result.is_error is False

        anyio.run(_run)


class TestSdkVersionPin:
    """Guards the dependency pin that was the entire root cause."""

    def test_installed_mcp_sdk_is_on_the_v2_line(self) -> None:
        """Reverting ``pyproject.toml`` to the v1 line reintroduces the defect
        wholesale, so the resolved major version is pinned explicitly.
        """
        pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
        with pyproject_path.open("rb") as pyproject_file:
            dependencies = tomllib.load(pyproject_file)["project"]["dependencies"]
        declared_mcp = next(
            dependency for dependency in dependencies if dependency.startswith("mcp==")
        )

        assert declared_mcp == "mcp==2.1.1"
        assert pkg_version("mcp") == declared_mcp.removeprefix("mcp==")
