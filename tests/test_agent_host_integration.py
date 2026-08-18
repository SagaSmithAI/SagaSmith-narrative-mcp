from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest


def test_real_sagasmith_agent_registry_refreshes_native_tools(tmp_path: Path) -> None:
    asyncio.run(_exercise_agent_host(tmp_path))


async def _exercise_agent_host(tmp_path: Path) -> None:
    pytest.importorskip("tiktoken", reason="run with SagaSmith-agent's Python environment")
    root = Path(__file__).parents[1]
    agent_root = root.parent / "SagaSmith-agent"
    sys.path.insert(0, str(agent_root))
    try:
        from nanobot.agent.tools.mcp import connect_mcp_servers
        from nanobot.agent.tools.registry import ToolRegistry
        from nanobot.config.schema import MCPServerConfig

        env = {
            "SAGASMITH_NARRATIVE_MCP_HOME": str(tmp_path / "home"),
            "PYTHONPATH": os.pathsep.join(
                [str(root / "src"), str(root.parent / "sagasmith-core" / "src")]
            ),
        }
        registry = ToolRegistry()
        narrative_python = root / ".venv" / (
            "Scripts/python.exe" if os.name == "nt" else "bin/python"
        )
        assert narrative_python.is_file(), "install Narrative MCP dev environment first"
        connections = await connect_mcp_servers(
            {
                "narrative": MCPServerConfig(
                    command=str(narrative_python),
                    args=["-m", "sagasmith_narrative_mcp.server"],
                    env=env,
                    cwd=str(root),
                    enabled_tools=["*"],
                )
            },
            registry,
        )
        try:
            assert "mcp_narrative_campaign_setup" not in registry.tool_names
            exposure = registry.get("mcp_narrative_exposure")
            assert exposure is not None
            await exposure.execute(action="open")
            await exposure.execute(action="set", add_tool_ids=["campaign_setup"])
            assert "mcp_narrative_campaign_setup" in registry.tool_names
        finally:
            for connection in connections.values():
                await connection.aclose()
    finally:
        sys.path.remove(str(agent_root))
