from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ServerNotification, ToolListChangedNotification

from sagasmith_narrative_mcp.policies import CORE_TOOLS


def value(result):
    assert not result.isError, result.content
    return json.loads(result.content[0].text)


def test_real_stdio_dynamic_tools_and_context_binding(tmp_path: Path) -> None:
    asyncio.run(_exercise_stdio(tmp_path))


async def _exercise_stdio(tmp_path: Path) -> None:
    notifications: list[str] = []

    async def handler(message):
        if isinstance(message, ServerNotification) and isinstance(
            message.root, ToolListChangedNotification
        ):
            notifications.append("tools/list_changed")

    env = dict(os.environ)
    env["SAGASMITH_NARRATIVE_MCP_HOME"] = str(tmp_path / "home")
    root = Path(__file__).parents[1]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), str(root.parent / "sagasmith-core" / "src")]
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sagasmith_narrative_mcp.server"],
        cwd=root,
        env=env,
    )
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, message_handler=handler) as session:
            await session.initialize()
            assert {item.name for item in (await session.list_tools()).tools} == set(CORE_TOOLS)
            value(await session.call_tool("exposure", {"action": "open"}))
            value(
                await session.call_tool(
                    "exposure", {"action": "set", "add_tool_ids": ["campaign_setup"]}
                )
            )
            assert "campaign_setup" in {item.name for item in (await session.list_tools()).tools}
            created = value(
                await session.call_tool(
                    "campaign_setup",
                    {
                        "action": "create",
                        "name": "Stdio Narrative",
                        "idempotency_key": "create",
                    },
                )
            )
            campaign_id = created["id"]
            value(
                await session.call_tool("exposure", {"action": "open", "campaign_id": campaign_id})
            )
            searched = value(
                await session.call_tool("exposure", {"action": "search", "query": "profile"})
            )
            assert [item["tool_id"] for item in searched["matches"]] == ["profile_change"]
            value(
                await session.call_tool(
                    "exposure",
                    {
                        "action": "set",
                        "add_tool_ids": ["profile_change", "branch_query"],
                    },
                )
            )
            campaign = value(
                await session.call_tool(
                    "campaign_query", {"action": "get", "campaign_id": campaign_id}
                )
            )
            branch = value(await session.call_tool("branch_query", {"campaign_id": campaign_id}))[
                "branches"
            ][0]
            drafted = value(
                await session.call_tool(
                    "profile_change",
                    {
                        "campaign_id": campaign_id,
                        "action": "create_draft",
                        "profile": {
                            "id": "profile.stdio",
                            "version": "1",
                            "title": "Stdio",
                            "mechanics_level": 0,
                            "sources": [{"kind": "original", "ref": "test"}],
                        },
                        "expected_revision": campaign["revision"],
                        "expected_branch_id": branch["id"],
                        "idempotency_key": "draft",
                    },
                )
            )
            binding = drafted["host_context_binding"]
            assert binding["campaign_id"] == campaign_id
            assert binding["branch_id"] == branch["id"]
            assert binding["phase"] == "lobby"
            assert notifications
