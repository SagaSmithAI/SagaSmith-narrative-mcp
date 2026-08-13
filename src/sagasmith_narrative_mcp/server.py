"""Native dynamic MCP surface for system-neutral narrative campaigns."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from dataclasses import asdict
from typing import Any, Literal
from uuid import uuid4
from weakref import WeakValueDictionary

import mcp.server.fastmcp.server as fastmcp_server_module
from mcp.server.fastmcp import FastMCP
from mcp.server.lowlevel.server import NotificationOptions
from mcp.types import TextContent
from sagasmith_core import IdempotencyWrite, default_local_principal
from sagasmith_core.access import LOCAL_SYSTEM_PRINCIPAL_ID
from sagasmith_core.database import Database

from .config import McpConfig
from .contracts import active_profile, narrative_document
from .exposure import Exposure, ExposureError, ExposureRegistry
from .policies import ADMIN_TOOLS, CORE_TOOLS, allowed_tools, policy_for_tool
from .runtime import ADMIN_ROLES, NarrativeRuntime
from .skills import SkillCatalog


class SessionExposureFastMCP(FastMCP):
    """Filter native tools/list and bind transport identity per MCP session."""

    def __init__(
        self,
        *args: Any,
        registry: ExposureRegistry,
        runtime: NarrativeRuntime,
        bound_principal_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        # mcp 1.29 ships its Settings generic with a deferred lifespan
        # annotation. pydantic-settings 2.15 correctly warns until the owning
        # module's namespace is supplied. Rebuild the model instead of hiding
        # the warning or pinning an older transitive dependency.
        settings_type = fastmcp_server_module.Settings
        if not settings_type.__pydantic_complete__:
            settings_type.model_rebuild(_types_namespace=vars(fastmcp_server_module))
        self.registry = registry
        self.runtime = runtime
        self._bound_principal_id = bound_principal_id.strip() if bound_principal_id else None
        self._sessions: WeakValueDictionary[str, Any] = WeakValueDictionary()
        self._exposure_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        self._campaign_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
        super().__init__(*args, **kwargs)
        original = self._mcp_server.create_initialization_options

        def options(notification_options=None, experimental_capabilities=None):
            return original(
                notification_options
                or NotificationOptions(
                    tools_changed=True, prompts_changed=False, resources_changed=False
                ),
                experimental_capabilities,
            )

        self._mcp_server.create_initialization_options = options

    def _request_session(self) -> tuple[str, Any] | None:
        try:
            session = self.get_context().session
        except (LookupError, ValueError):
            return None
        key = getattr(session, "_sagasmith_narrative_session_key", None)
        if key is None:
            key = f"mcp:{uuid4().hex}"
            setattr(session, "_sagasmith_narrative_session_key", key)
        self._sessions[key] = session
        return key, session

    def _principal_argument(self, name: str) -> str | None:
        tool = self._tool_manager.get_tool(name)
        properties = dict((tool.parameters if tool else {}).get("properties") or {})
        for candidate in ("principal_id", "by_principal_id"):
            if candidate in properties:
                return candidate
        return None

    def _bind_principal(
        self, name: str, arguments: dict[str, Any], exposure: Exposure | None
    ) -> dict[str, Any]:
        result = dict(arguments)
        argument = self._principal_argument(name)
        expected = self._bound_principal_id or (exposure.principal_id if exposure else None)
        if argument is None or expected is None:
            return result
        supplied = result.get(argument)
        if supplied is not None and supplied != expected and self._bound_principal_id is None:
            raise ExposureError("tool principal does not match session exposure")
        result[argument] = expected
        return result

    def _allowed(self, exposure: Exposure) -> set[str]:
        document = (
            narrative_document(self.runtime.campaigns.get(exposure.campaign_id).state)
            if exposure.campaign_id
            else None
        )
        profile = active_profile(document) if document else None
        capabilities = set(profile.get("capabilities", [])) if profile else set()
        result = allowed_tools(exposure.phase, capabilities)
        if exposure.campaign_id:
            membership = self.runtime.access.require_campaign(
                exposure.campaign_id, exposure.principal_id
            )
            if membership.role not in ADMIN_ROLES:
                result -= set(ADMIN_TOOLS)
                if not self.runtime.principal_controls_actor(
                    exposure.campaign_id, exposure.principal_id
                ):
                    result.discard("actor_change")
            assert document is not None
            authority = dict(profile.get("authority") or {}) if profile else {}
            facilitator = self.runtime._has_facilitator_authority(document, membership.role)
            actor_conflict_authority = bool(
                authority.get("player_controls_owned_actor")
            ) and self.runtime.principal_controls_actor(exposure.campaign_id, exposure.principal_id)
            current_conflict = document.get("conflict")
            owns_conflict = bool(
                current_conflict
                and current_conflict.get("controller_principal_id") == exposure.principal_id
            )
            if not facilitator and not actor_conflict_authority and not owns_conflict:
                result -= {
                    "conflict_start",
                    "conflict_query",
                    "conflict_act",
                    "conflict_end",
                }
            if membership.role == "observer":
                result -= {
                    name
                    for name in result
                    if name.endswith("_change")
                    or name.endswith("_settle")
                    or name
                    in {
                        "game_phase",
                        "mechanic_resolve",
                        "npc_conversation",
                        "narrative_settle",
                        "conflict_start",
                        "conflict_act",
                        "conflict_end",
                    }
                }
        return result

    async def _refresh(self, campaign_id: str | None = None) -> bool:
        changed = []
        candidates = (
            self.registry.for_campaign(campaign_id)
            if campaign_id
            else list(self.registry._items.values())
        )
        for exposure in candidates:
            if exposure.campaign_id is None:
                continue
            phase = self.runtime.phase(exposure.campaign_id)
            if self.registry.refresh(exposure, phase, self._allowed(exposure)):
                changed.append(exposure.session_key)
        for session_key in changed:
            session = self._sessions.get(session_key)
            if session is not None:
                await session.send_tool_list_changed()
        return bool(changed)

    @staticmethod
    def _attach(result: Any, binding: dict[str, Any] | None) -> Any:
        if binding is None or not (isinstance(result, tuple) and len(result) == 2):
            return result
        content, structured = result

        def apply(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            output = deepcopy(value)
            payload = output.get("result")
            if isinstance(payload, dict):
                payload["host_context_binding"] = deepcopy(binding)
            else:
                output["host_context_binding"] = deepcopy(binding)
            return output

        changed_content = []
        for item in content:
            if not isinstance(item, TextContent):
                changed_content.append(item)
                continue
            try:
                decoded = json.loads(item.text)
            except json.JSONDecodeError:
                changed_content.append(item)
                continue
            changed_content.append(
                item.model_copy(
                    update={
                        "text": json.dumps(
                            apply(decoded), ensure_ascii=False, separators=(",", ":")
                        )
                    }
                )
            )
        return changed_content, apply(structured)

    async def list_tools(self):  # type: ignore[override]
        request = self._request_session()
        if request is None:
            return [tool for tool in await super().list_tools() if tool.name in CORE_TOOLS]
        session_key, _ = request
        await self._refresh()
        visible = self.registry.visible(self.registry.active(session_key))
        return [tool for tool in await super().list_tools() if tool.name in visible]

    async def call_tool(self, name: str, arguments: dict[str, Any]):  # type: ignore[override]
        request = self._request_session()
        if request is None:
            if name not in CORE_TOOLS:
                raise ExposureError("domain tools require a real MCP session exposure")
            return await super().call_tool(name, arguments)
        session_key, _ = request
        await self._refresh()
        exposure = self.registry.active(session_key)
        arguments = self._bind_principal(name, arguments, exposure)
        if name not in CORE_TOOLS:
            if exposure is None:
                raise ExposureError("open a session exposure before calling domain tools")
            self.registry.require(exposure, name)
            campaign_arg = str(arguments.get("campaign_id") or "") or None
            if exposure.campaign_id != campaign_arg:
                raise ExposureError("tool campaign does not match session exposure")
            policy = policy_for_tool(name)
            if policy and policy.admin_only:
                self.runtime.access.require_campaign(
                    campaign_arg, exposure.principal_id, roles=ADMIN_ROLES
                )
            async with self._exposure_locks.setdefault(exposure.id, asyncio.Lock()):
                if campaign_arg:
                    async with self._campaign_locks.setdefault(campaign_arg, asyncio.Lock()):
                        result = await super().call_tool(name, arguments)
                else:
                    result = await super().call_tool(name, arguments)
        else:
            result = await super().call_tool(name, arguments)
        if name == "exposure" and arguments.get("action") in {"open", "set"}:
            session = self._sessions.get(session_key)
            if session is not None:
                await session.send_tool_list_changed()
        campaign_id = str(arguments.get("campaign_id") or "") or None
        campaign_id = campaign_id or (exposure.campaign_id if exposure else None)
        if campaign_id:
            await self._refresh(campaign_id)
            principal = (
                exposure.principal_id if exposure else str(arguments.get("principal_id") or "")
            )
            binding = self.runtime.binding(campaign_id, principal)
            current = self.registry.active(session_key)
            binding["exposure_revision"] = current.revision if current else None
            result = self._attach(result, binding)
        return result


def create_server(config: McpConfig | None = None) -> FastMCP:
    config = config or McpConfig.from_environment()
    database = Database(config.database_url)
    database.create_schema()
    default_local_principal(database)
    runtime = NarrativeRuntime(database)
    registry = ExposureRegistry()
    skills = SkillCatalog()
    mcp = SessionExposureFastMCP(
        "SagaSmith Narrative",
        registry=registry,
        runtime=runtime,
        bound_principal_id=config.bound_principal_id,
    )

    def common(
        principal_id: str,
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return {
            "principal_id": principal_id,
            "expected_revision": expected_revision,
            "expected_branch_id": expected_branch_id,
            "idempotency_key": idempotency_key,
        }

    @mcp.tool()
    def server_capabilities() -> dict[str, Any]:
        return {
            "system_id": "narrative",
            "schema_version": 1,
            "base_phases": ["lobby", "play"],
            "optional_phases": ["conflict"],
            "mechanics_levels": [0, 1],
            "native_dynamic_tools": True,
            "tools_list_changed": True,
            "fixed_superset": False,
            "compatibility_fallback": False,
            "identity_mode": (
                "process_bound_principal"
                if config.bound_principal_id
                else "trusted_single_user_local_stdio"
            ),
            "shared_network_transport_supported": False,
        }

    @mcp.tool()
    def campaign_query(
        action: Literal["list", "get"],
        campaign_id: str | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        if action == "list":
            return {"campaigns": runtime.campaign_list(principal_id)}
        if not campaign_id:
            raise ValueError("campaign_id is required")
        return runtime.campaign_get(campaign_id, principal_id)

    @mcp.tool()
    def skill_query(
        action: Literal["list", "get_section", "search"],
        skill_id: str | None = None,
        section: str | None = None,
        query: str = "",
    ) -> dict[str, Any]:
        if action == "list":
            return {"skills": skills.list()}
        if action == "search":
            return skills.search(query)
        if not skill_id:
            raise ValueError("skill_id is required")
        if section is None:
            return {"skill_id": skill_id, "sections": skills.sections(skill_id)}
        return skills.get_section(skill_id, section)

    @mcp.tool()
    async def exposure(
        action: Literal["open", "get", "search", "set"],
        campaign_id: str | None = None,
        query: str = "",
        add_tool_ids: list[str] | None = None,
        remove_tool_ids: list[str] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        request = mcp._request_session()
        session_key = request[0] if request else f"direct:{principal_id}"
        if config.bound_principal_id:
            principal_id = config.bound_principal_id
        if action == "open":
            phase = "lobby"
            if campaign_id:
                runtime.access.require_campaign(campaign_id, principal_id)
                phase = runtime.phase(campaign_id)
            return registry.status(registry.open(session_key, principal_id, campaign_id, phase))
        current = registry.active(session_key)
        if current is None:
            raise ExposureError("no active exposure; use action='open'")
        if action == "get":
            return registry.status(current)
        allowed = mcp._allowed(current)
        if action == "search":
            terms = [item.casefold() for item in query.split() if item.strip()]
            matches = []
            for tool in mcp._tool_manager.list_tools():
                if tool.name not in allowed or tool.name in CORE_TOOLS:
                    continue
                haystack = f"{tool.name} {tool.description or ''}".casefold()
                if terms and not all(term in haystack for term in terms):
                    continue
                matches.append(
                    {
                        "tool_id": tool.name,
                        "description": tool.description or "",
                        "loaded": tool.name in current.loaded_tools,
                    }
                )
            return {**registry.status(current), "matches": matches}
        changed = registry.set_tools(current, add_tool_ids or [], remove_tool_ids or [], allowed)
        return {**registry.status(current), "changed": changed}

    @mcp.tool()
    def campaign_setup(
        action: Literal["create"],
        name: str,
        description: str = "",
        slug: str | None = None,
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.campaign_create(
            name=name,
            description=description,
            slug=slug,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
        )

    @mcp.tool()
    def access_change(
        campaign_id: str,
        action: Literal[
            "campaign_grant",
            "campaign_revoke",
            "actor_grant",
            "actor_revoke",
            "element_grant",
            "element_revoke",
        ],
        target_principal_id: str,
        role: str | None = None,
        actor_id: str | None = None,
        can_control: bool = False,
        can_view_private: bool = False,
        element_ref: str | None = None,
        scope: dict[str, Any] | None = None,
        expected_revision: int = 0,
        expected_branch_id: str = "",
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.access_change(
            campaign_id,
            principal_id=principal_id,
            action=action,
            target_principal_id=target_principal_id,
            role=role,
            actor_id=actor_id,
            can_control=can_control,
            can_view_private=can_view_private,
            element_ref=element_ref,
            scope=scope,
            expected_revision=expected_revision,
            expected_branch_id=expected_branch_id,
            idempotency_key=idempotency_key,
        )

    @mcp.tool()
    def profile_change(
        campaign_id: str,
        action: Literal["create_draft", "update_draft", "finalize", "activate"],
        profile: dict[str, Any] | None = None,
        profile_key: str | None = None,
        expected_revision: int = 0,
        expected_branch_id: str = "",
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.profile_change(
            campaign_id,
            action=action,
            profile=profile,
            profile_key=profile_key,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def pack_change(
        campaign_id: str,
        action: Literal[
            "create_draft", "update_draft", "finalize", "import", "activate", "apply_seed"
        ],
        pack: dict[str, Any] | None = None,
        pack_key: str | None = None,
        expected_revision: int = 0,
        expected_branch_id: str = "",
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        if action == "apply_seed":
            return runtime.campaign_seed_apply(
                campaign_id,
                pack_key=str(pack_key),
                principal_id=principal_id,
                expected_revision=expected_revision,
                expected_branch_id=expected_branch_id,
                idempotency_key=idempotency_key,
            )
        return runtime.pack_change(
            campaign_id,
            action=action,
            pack=pack,
            pack_key=pack_key,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def game_phase(
        campaign_id: str,
        phase: Literal["lobby", "play"],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.set_phase(
            campaign_id,
            phase=phase,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def actor_query(
        campaign_id: str, actor_id: str | None = None, principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID
    ) -> dict[str, Any]:
        return runtime.actor_query(campaign_id, principal_id=principal_id, actor_id=actor_id)

    @mcp.tool()
    def actor_change(
        campaign_id: str,
        action: Literal["create", "update"],
        actor: dict[str, Any],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        actor_id: str | None = None,
        expected_actor_revision: int | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        if action == "update":
            if not actor_id or expected_actor_revision is None:
                raise ValueError("actor update requires actor_id and expected_actor_revision")
            return runtime.actor_update(
                campaign_id,
                principal_id=principal_id,
                actor_id=actor_id,
                actor=actor,
                expected_actor_revision=expected_actor_revision,
                expected_revision=expected_revision,
                expected_branch_id=expected_branch_id,
                idempotency_key=idempotency_key,
            )
        return runtime.actor_create(
            campaign_id,
            principal_id=principal_id,
            idempotency_key=idempotency_key,
            actor=actor,
            expected_revision=expected_revision,
            expected_branch_id=expected_branch_id,
        )

    @mcp.tool()
    def scene_change(
        campaign_id: str,
        action: Literal["start", "update", "end"],
        scene: dict[str, Any] | None = None,
        scene_id: str | None = None,
        expected_revision: int = 0,
        expected_branch_id: str = "",
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.scene_change(
            campaign_id,
            action=action,
            scene=scene,
            scene_id=scene_id,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def narrative_query(
        campaign_id: str,
        kind: Literal["profile", "pack", "scene", "record"],
        record_id: str | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.query(campaign_id, principal_id=principal_id, kind=kind, record_id=record_id)

    @mcp.tool()
    def narrative_change(
        campaign_id: str,
        action: Literal["create", "update"],
        record: dict[str, Any],
        expected_record_revision: int | None = None,
        expected_revision: int = 0,
        expected_branch_id: str = "",
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.record_change(
            campaign_id,
            action=action,
            record=record,
            expected_record_revision=expected_record_revision,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def narrative_settle(
        campaign_id: str,
        event: dict[str, Any],
        record_changes: list[dict[str, Any]] | None,
        facts: list[dict[str, Any]] | None,
        actor_knowledge: list[dict[str, Any]] | None,
        snapshot: dict[str, Any] | None,
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.narrative_settle(
            campaign_id,
            principal_id=principal_id,
            expected_revision=expected_revision,
            expected_branch_id=expected_branch_id,
            idempotency_key=idempotency_key,
            event=event,
            record_changes=record_changes,
            facts=facts,
            actor_knowledge=actor_knowledge,
            snapshot=snapshot,
        )

    @mcp.tool()
    def continuity_query(
        campaign_id: str,
        actor_id: str | None = None,
        limit: int = 50,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        membership = runtime.access.require_campaign(campaign_id, principal_id)
        if actor_id:
            actor_id = runtime.require_actor_authority(campaign_id, actor_id, principal_id)
        elif membership.role not in ADMIN_ROLES:
            visible_actors = runtime.actor_query(campaign_id, principal_id=principal_id).get(
                "actors", []
            )
            if len(visible_actors) == 1:
                actor_id = str(visible_actors[0]["id"])
        document = narrative_document(runtime.campaigns.get(campaign_id).state)
        audience = (
            "dm" if runtime._has_facilitator_authority(document, membership.role) else "player"
        )
        return runtime.continuity.context(
            campaign_id, actor_id=actor_id, audience=audience, limit=limit
        )

    @mcp.tool()
    def mechanic_resolve(
        campaign_id: str,
        mechanic_id: str,
        inputs: dict[str, Any],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.mechanic_resolve(
            campaign_id,
            mechanic_id=mechanic_id,
            inputs=inputs,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def npc_conversation(
        campaign_id: str,
        action: Literal["open", "propose", "publish", "close", "abort"],
        conversation_id: str | None = None,
        npc_actor_id: str | None = None,
        data: dict[str, Any] | None = None,
        expected_revision: int = 0,
        expected_branch_id: str = "",
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.npc_conversation(
            campaign_id,
            action=action,
            conversation_id=conversation_id,
            npc_actor_id=npc_actor_id,
            data=data,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def downtime_settle(
        campaign_id: str,
        summary: str,
        changes: list[dict[str, Any]],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        facts: list[dict[str, Any]] | None = None,
        actor_knowledge: list[dict[str, Any]] | None = None,
        snapshot: dict[str, Any] | None = None,
        audience_scope: str = "public",
        participants: list[dict[str, Any]] | None = None,
        payload: dict[str, Any] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.activity_settle(
            campaign_id,
            activity="downtime",
            summary=summary,
            changes=changes,
            facts=facts,
            actor_knowledge=actor_knowledge,
            snapshot=snapshot,
            audience_scope=audience_scope,
            participants=participants,
            payload=payload,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def world_turn_settle(
        campaign_id: str,
        summary: str,
        changes: list[dict[str, Any]],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        facts: list[dict[str, Any]] | None = None,
        actor_knowledge: list[dict[str, Any]] | None = None,
        snapshot: dict[str, Any] | None = None,
        audience_scope: str = "public",
        participants: list[dict[str, Any]] | None = None,
        payload: dict[str, Any] | None = None,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.activity_settle(
            campaign_id,
            activity="world_turn",
            summary=summary,
            changes=changes,
            facts=facts,
            actor_knowledge=actor_knowledge,
            snapshot=snapshot,
            audience_scope=audience_scope,
            participants=participants,
            payload=payload,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def conflict_start(
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.conflict(
            campaign_id,
            action="start",
            data=data,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def conflict_query(
        campaign_id: str, principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID
    ) -> dict[str, Any]:
        runtime.access.require_campaign(campaign_id, principal_id)
        return {
            "conflict": narrative_document(runtime.campaigns.get(campaign_id).state).get("conflict")
        }

    @mcp.tool()
    def conflict_act(
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.conflict(
            campaign_id,
            action="act",
            data=data,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def conflict_end(
        campaign_id: str,
        data: dict[str, Any],
        expected_revision: int,
        expected_branch_id: str,
        idempotency_key: str,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        return runtime.conflict(
            campaign_id,
            action="end",
            data=data,
            **common(principal_id, expected_revision, expected_branch_id, idempotency_key),
        )

    @mcp.tool()
    def snapshot_query(
        campaign_id: str, principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID
    ) -> dict[str, Any]:
        runtime.access.require_campaign(campaign_id, principal_id, roles=ADMIN_ROLES)
        return {"snapshots": [asdict(item) for item in runtime.snapshots.list(campaign_id)]}

    @mcp.tool()
    def snapshot_change(
        campaign_id: str,
        action: Literal["create", "restore"],
        label: str = "",
        slot: int | None = None,
        expected_revision: int = 0,
        expected_branch_id: str = "",
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        runtime.access.require_campaign(campaign_id, principal_id, roles=ADMIN_ROLES)
        if action == "restore" and slot is None:
            raise ValueError("snapshot restore requires slot")
        if action == "create":
            return runtime.snapshot_create(
                campaign_id,
                label=label,
                principal_id=principal_id,
                expected_revision=expected_revision,
                expected_branch_id=expected_branch_id,
                idempotency_key=idempotency_key,
            )
        runtime.require_no_open_npc_conversation(campaign_id)
        scope = f"narrative:snapshot:{action}:{campaign_id}:{expected_branch_id}:{principal_id}"
        payload = {
            "action": action,
            "label": label,
            "slot": slot,
            "expected_revision": expected_revision,
        }
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        replay = runtime.idempotency.lookup(scope, key, payload)
        if replay is not None and replay.response is not None:
            return deepcopy(replay.response)
        write = IdempotencyWrite(scope, payload, lambda result: {"snapshot": asdict(result)})
        result = runtime.snapshots.restore(
            campaign_id,
            int(slot),
            expected_revision=expected_revision,
            expected_branch_id=expected_branch_id,
            idempotency_key=key,
            idempotency_write=write,
        )
        return {"snapshot": asdict(result)}

    @mcp.tool()
    def branch_query(
        campaign_id: str, principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID
    ) -> dict[str, Any]:
        runtime.access.require_campaign(campaign_id, principal_id, roles=ADMIN_ROLES)
        return {"branches": [asdict(item) for item in runtime.branches.list(campaign_id)]}

    @mcp.tool()
    def branch_change(
        campaign_id: str,
        action: Literal["create", "checkout"],
        name: str = "",
        branch_id: str | None = None,
        checkout: bool = False,
        expected_revision: int = 0,
        expected_branch_id: str = "",
        idempotency_key: str = "",
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        runtime.access.require_campaign(campaign_id, principal_id, roles=ADMIN_ROLES)
        runtime.require_no_open_npc_conversation(campaign_id)
        scope = f"narrative:branch:{action}:{campaign_id}:{expected_branch_id}:{principal_id}"
        payload = {
            "action": action,
            "name": name,
            "branch_id": branch_id,
            "checkout": checkout,
            "expected_revision": expected_revision,
        }
        key = str(idempotency_key or "").strip()
        if not key:
            raise ValueError("idempotency_key is required")
        replay = runtime.idempotency.lookup(scope, key, payload)
        if replay is not None and replay.response is not None:
            return deepcopy(replay.response)

        def branch_response(result: Any) -> dict[str, Any]:
            value = result.get("branch") if isinstance(result, dict) else result
            return {"branch": asdict(value)}

        write = IdempotencyWrite(scope, payload, branch_response)
        if action == "checkout" and not branch_id:
            raise ValueError("branch checkout requires branch_id")
        result = (
            runtime.branches.create(
                campaign_id,
                name=name,
                checkout=checkout,
                expected_revision=expected_revision,
                expected_branch_id=expected_branch_id,
                idempotency_key=key,
                idempotency_write=write,
            )
            if action == "create"
            else runtime.branches.checkout(
                campaign_id,
                str(branch_id),
                expected_revision=expected_revision,
                expected_branch_id=expected_branch_id,
                idempotency_key=key,
                idempotency_write=write,
            )
        )
        return {"branch": asdict(result)}

    @mcp.tool()
    def state_revision(
        campaign_id: str,
        action: Literal["list"],
        limit: int = 100,
        principal_id: str = LOCAL_SYSTEM_PRINCIPAL_ID,
    ) -> dict[str, Any]:
        runtime.access.require_campaign(campaign_id, principal_id, roles=ADMIN_ROLES)
        return {
            "revisions": [
                asdict(item) for item in runtime.revisions.history(campaign_id, limit=limit)
            ]
        }

    return mcp


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
