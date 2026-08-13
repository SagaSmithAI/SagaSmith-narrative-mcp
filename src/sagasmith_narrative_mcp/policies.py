"""One current dynamic tool policy."""

from __future__ import annotations

from dataclasses import dataclass

LOBBY = "lobby"
PLAY = "play"
CONFLICT = "conflict"
CORE_TOOLS = frozenset({"exposure", "server_capabilities", "campaign_query", "skill_query"})

LOBBY_TOOLS = frozenset(
    "campaign_setup access_change profile_change pack_change actor_change actor_query "
    "game_phase narrative_query snapshot_change snapshot_query branch_change branch_query "
    "state_revision".split()
)
PLAY_TOOLS = frozenset(
    "access_change actor_change actor_query game_phase scene_change continuity_query "
    "narrative_query "
    "narrative_change narrative_settle mechanic_resolve npc_conversation downtime_settle "
    "world_turn_settle snapshot_change snapshot_query branch_change branch_query "
    "state_revision conflict_query".split()
)
CONFLICT_TOOLS = frozenset(
    "actor_query campaign_query continuity_query narrative_query mechanic_resolve conflict_query "
    "conflict_act conflict_end snapshot_query branch_query".split()
)
OPTIONAL_CONFLICT_TOOLS = frozenset(
    {"conflict_start", "conflict_query", "conflict_act", "conflict_end"}
)
ADMIN_TOOLS = frozenset(
    "access_change profile_change pack_change game_phase snapshot_change snapshot_query "
    "branch_change branch_query state_revision".split()
)
NO_CAMPAIGN_TOOLS = frozenset({"campaign_setup"})


@dataclass(frozen=True)
class ToolPolicy:
    id: str
    phases: frozenset[str]
    requires_campaign: bool = True
    admin_only: bool = False
    capability: str | None = None


def _build() -> dict[str, ToolPolicy]:
    all_tools = LOBBY_TOOLS | PLAY_TOOLS | CONFLICT_TOOLS | {"conflict_start"}
    result = {}
    for name in all_tools:
        phases = set()
        if name in LOBBY_TOOLS:
            phases.add(LOBBY)
        if name in PLAY_TOOLS or name == "conflict_start":
            phases.add(PLAY)
        if name in CONFLICT_TOOLS:
            phases.add(CONFLICT)
        result[name] = ToolPolicy(
            name,
            frozenset(phases),
            requires_campaign=name not in NO_CAMPAIGN_TOOLS,
            admin_only=name in ADMIN_TOOLS,
            capability=(
                "conflict"
                if name in OPTIONAL_CONFLICT_TOOLS
                else (
                    "mechanics"
                    if name == "mechanic_resolve"
                    else (
                        "npc_conversation"
                        if name == "npc_conversation"
                        else (
                            "downtime"
                            if name == "downtime_settle"
                            else ("world_turn" if name == "world_turn_settle" else None)
                        )
                    )
                )
            ),
        )
    return result


TOOL_POLICIES = _build()


def policy_for_tool(name: str) -> ToolPolicy | None:
    return TOOL_POLICIES.get(name)


def allowed_tools(phase: str, capabilities: set[str]) -> set[str]:
    result = set(CORE_TOOLS)
    for policy in TOOL_POLICIES.values():
        if phase not in policy.phases:
            continue
        if policy.capability and policy.capability not in capabilities:
            continue
        result.add(policy.id)
    return result
