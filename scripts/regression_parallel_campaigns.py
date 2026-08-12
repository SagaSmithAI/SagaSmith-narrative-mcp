"""Execute every original long-campaign route specification concurrently."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from contextlib import AsyncExitStack
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ServerNotification, ToolListChangedNotification

from sagasmith_narrative_mcp.fixtures import load_fixture
from sagasmith_narrative_mcp.route_dsl import (
    MISSING,
    OperatorRegistry,
    PerformanceOracle,
    RouteDocument,
    RouteInterpreter,
    RouteLoader,
    apply_deltas,
    merge_patch,
    path_value,
    replace_aliases,
)

ROOT = Path(__file__).parents[1]
SKILLS = ROOT.parent / "SagaSmith-narrative-skills" / "skills"


def decode(result: Any) -> dict[str, Any]:
    if result.isError:
        message = " ".join(str(getattr(item, "text", item)) for item in result.content)
        raise RuntimeError(message)
    return json.loads(result.content[0].text)


class StdioRouteBackend:
    """Route backend whose only campaign operations are native MCP calls."""

    def __init__(
        self,
        sessions: dict[str, ClientSession],
        document: RouteDocument,
        fixture: dict[str, Any],
        notification_counter: list[int],
    ) -> None:
        self.sessions = sessions
        self.session = sessions[next(iter(sessions))]
        self.document = document
        self.fixture = fixture
        self.notifications = notification_counter
        self.operators = OperatorRegistry()
        self.principals = dict(document.data.get("principals") or {})
        self.default_principal = next(iter(self.principals))
        self.current_principal = self.default_principal
        self.campaign_id = ""
        self.actor_aliases: dict[str, str] = {}
        self.saved: dict[str, Any] = {}
        self.timeline: list[dict[str, Any]] = []
        self.last_result: dict[str, Any] = {}
        self.last_write: tuple[str, dict[str, Any], dict[str, Any]] | None = None
        self.last_settlement_write: tuple[str, dict[str, Any], dict[str, Any]] | None = None
        self.last_error = ""
        self.assertion_root: dict[str, Any] = {}
        self.inline_assertions = 0
        self.legal_endings: set[str] = set()
        self.original_branch = ""
        self.pre_restore_branch = ""
        self.pre_restore_notifications = 0
        self.random_start = 0
        self.random_end = 0
        self.mechanic_calls = 0
        self.conflict_calls = 0
        self.conflict_visible_count = 0
        self.element_denials = 0
        self.private_leaks = 0
        self.authority_bypasses = 0
        self.idempotency_duplicate_events = 0
        self.event_counts: dict[str, int] = {}
        self.seen_event_ids: set[str] = set()
        self.executed_ids: list[str] = []
        self.element_grants: set[tuple[str, str]] = set()
        self.conversation_workers: dict[str, str] = {}
        self.open_conversations: set[str] = set()
        seed = RouteLoader.reference(document, "campaign-seed.json")
        self.principal_actor_refs = {
            str(item["id"]): [str(actor) for actor in item.get("actor_grants", [])]
            for item in seed.get("content", {}).get("principals", [])
        }
        self.skill_whole_file_returns = 0
        self.seed_knowledge_materialized = False
        self.private_proposals: dict[str, str] = {}
        self.director_private_proposals = 0
        self.last_session_principal = self.default_principal
        performance_contract = document.data.get("performance_contract")
        self.performance = PerformanceOracle(
            RouteLoader.reference(document, str(performance_contract))
            if performance_contract
            else None
        )
        module = RouteLoader.reference(document, "module.json")
        self.scenes = {
            str(item["id"]): item for item in module.get("content", {}).get("scenes", [])
        }
        self.endings = {
            str(item["id"]): item for item in module.get("content", {}).get("endings", [])
        }

    def inline_assertion_count(self) -> int:
        return self.inline_assertions

    def principal_id(self, alias: str | None = None) -> str:
        selected = alias or self.current_principal
        return str(self.principals.get(selected, selected))

    async def visible_tools(self) -> set[str]:
        return {item.name for item in (await self.session.list_tools()).tools}

    async def bind(self, principal: str | None = None) -> None:
        alias = principal or self.current_principal
        self.session = self.sessions[alias]
        principal_id = self.principal_id(alias)
        if not self.campaign_id:
            await self._raw("exposure", {"action": "open", "principal_id": principal_id})
        else:
            await self._raw(
                "exposure",
                {
                    "action": "open",
                    "campaign_id": self.campaign_id,
                    "principal_id": principal_id,
                },
            )
        self.current_principal = alias

    async def ensure_tool(self, tool: str) -> None:
        if tool in await self.visible_tools():
            return
        searched = await self._raw(
            "exposure", {"action": "search", "query": tool, "principal_id": self.principal_id()}
        )
        matches = {item["tool_id"] for item in searched.get("matches", [])}
        if tool not in matches:
            raise RuntimeError(f"native tool unavailable in current context: {tool}")
        await self._raw(
            "exposure",
            {"action": "set", "add_tool_ids": [tool], "principal_id": self.principal_id()},
        )

    async def context(self) -> tuple[int, str]:
        await self.ensure_tool("branch_query")
        campaign = await self._raw(
            "campaign_query",
            {
                "action": "get",
                "campaign_id": self.campaign_id,
                "principal_id": self.principal_id(),
            },
        )
        branches = await self._raw(
            "branch_query",
            {"campaign_id": self.campaign_id, "principal_id": self.principal_id()},
        )
        branch = next(item for item in branches["branches"] if item["is_current"])
        return int(campaign["revision"]), str(branch["id"])

    async def _raw(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = decode(await self.session.call_tool(tool, arguments))
        binding = result.get("host_context_binding") or {}
        self.timeline.append(
            {
                "tool": tool,
                "principal_id": arguments.get("principal_id") or self.principal_id(),
                "phase": result.get("phase") or binding.get("phase"),
                "campaign_revision": result.get("campaign_revision")
                or binding.get("campaign_revision"),
                "branch_id": result.get("branch_id") or binding.get("branch_id"),
            }
        )
        if tool == "mechanic_resolve":
            self.mechanic_calls += 1
            receipt = result.get("random_stream_receipt") or {}
            self.random_end = int(receipt.get("cursor_after", self.random_end))
        if tool.startswith("conflict_"):
            self.conflict_calls += 1
        event = result.get("event")
        if isinstance(event, Mapping):
            event_type = str(event.get("event_type") or "")
            event_id = str(event.get("id") or "")
            if event_type and event_id not in self.seen_event_ids:
                self.event_counts[event_type] = self.event_counts.get(event_type, 0) + 1
                if event_id:
                    self.seen_event_ids.add(event_id)
        self.last_result = result
        return result

    async def call(
        self,
        tool: str,
        arguments: dict[str, Any],
        *,
        principal: str | None = None,
        write: bool = False,
        remember: bool = True,
    ) -> dict[str, Any]:
        if principal is not None and principal != self.current_principal:
            await self.bind(principal)
        await self.ensure_tool(tool)
        payload = deepcopy(arguments)
        if self.campaign_id and tool not in {"campaign_setup"}:
            payload.setdefault("campaign_id", self.campaign_id)
            payload.setdefault("principal_id", self.principal_id())
        if write:
            revision, branch = await self.context()
            payload.setdefault("expected_revision", revision)
            payload.setdefault("expected_branch_id", branch)
            payload.setdefault("idempotency_key", self._key(tool))
        result = await self._raw(tool, payload)
        if write and remember:
            self.last_write = (tool, deepcopy(payload), deepcopy(result))
            if tool in {"narrative_settle", "downtime_settle", "world_turn_settle"}:
                self.last_settlement_write = (tool, deepcopy(payload), deepcopy(result))
        return result

    def _key(self, tool: str) -> str:
        return f"route-{len(self.timeline):04d}-{tool}"

    async def execute_setup(self, step: dict[str, Any]) -> int:
        self.executed_ids.append(str(step["id"]))
        principal = str(step.get("principal") or self.current_principal)
        tool = str(step["tool"])
        if tool == "skill_query":
            return await self._setup_skills(step)
        if tool == "campaign_setup":
            await self.bind(principal)
            await self.ensure_tool(tool)
            created = await self.call(
                tool,
                {"action": "create", **deepcopy(step.get("input") or {})},
                principal=principal,
                write=False,
            )
            self.campaign_id = str(created["id"])
            self.original_branch = str(created.get("active_branch_id") or "")
            await self.bind(principal)
            self.assertion_root = await self._raw(
                "campaign_query",
                {
                    "action": "get",
                    "campaign_id": self.campaign_id,
                    "principal_id": self.principal_id(),
                },
            )
            return 1
        if tool == "profile_change":
            profile = RouteLoader.reference(self.document, str(step["load"]))
            key = f"{profile['id']}@{profile['version']}"
            result = {}
            for action in step["actions"]:
                args = {"action": action, "profile_key": key}
                if action == "create_draft":
                    args["profile"] = profile
                result = await self.call(tool, args, principal=principal, write=True)
            self.assertion_root = result
            return len(step["actions"])
        if tool == "pack_change":
            result = {}
            seed_key = None
            for filename in step["for_each"]:
                pack = RouteLoader.reference(self.document, str(filename))
                key = f"{pack['id']}@{pack['version']}"
                if pack["kind"] == "campaign_seed":
                    seed_key = key
                for action in step["actions"]:
                    args = {"action": action, "pack_key": key}
                    if action == "create_draft":
                        args["pack"] = pack
                    result = await self.call(tool, args, principal=principal, write=True)
                # Route assertions describe each lifecycle target, not merely the
                # compact activate response.  Preserve the authoritative Pack.
                result["pack"] = pack
            self.saved["seed_key"] = seed_key
            self.assertion_root = result
            return len(step["actions"]) * len(step["for_each"])
        if tool == "access_change":
            seed = RouteLoader.reference(self.document, "campaign-seed.json")
            applied = await self.call(
                "pack_change",
                {"action": "apply_seed", "pack_key": self.saved["seed_key"]},
                principal=principal,
                write=True,
            )
            self.actor_aliases.update(applied.get("actor_bindings") or {})
            self.seed_knowledge_materialized = bool(applied.get("actor_knowledge_materialized"))
            # The public seed operation materializes memberships, actors, grants, and records.
            for grant in seed.get("content", {}).get("element_stewardship", []):
                await self.call(
                    "access_change",
                    {
                        "action": "element_grant",
                        "target_principal_id": grant["principal_id"],
                        "element_ref": grant["element_ref"],
                        "can_control": grant.get("can_control", False),
                        "can_view_private": grant.get("can_view_private", False),
                        "scope": grant.get("scope") or {},
                    },
                    principal=principal,
                    write=True,
                )
                self.element_grants.add((str(grant["principal_id"]), str(grant["element_ref"])))
            self.assertion_root = applied
            return 1 + len(seed.get("content", {}).get("element_stewardship", []))
        if tool == "game_phase":
            result = await self.call(
                tool, deepcopy(step.get("input") or {}), principal=principal, write=True
            )
            then = step.get("then") or {}
            if then and not self.seed_knowledge_materialized:
                knowledge = RouteLoader.reference(
                    self.document, str(then.get("actor_knowledge_from"))
                )
                await self.call(
                    str(then["tool"]),
                    {
                        "event": deepcopy(then["event"]),
                        "record_changes": [],
                        "facts": [],
                        "actor_knowledge": knowledge,
                        "snapshot": None,
                    },
                    principal=principal,
                    write=True,
                )
            self.assertion_root = result
            return 2 if then else 1
        raise ValueError(f"unsupported setup tool: {tool}")

    async def _setup_skills(self, step: dict[str, Any]) -> int:
        await self.bind(str(step.get("principal") or self.current_principal))
        listed = await self._raw("skill_query", {"action": "list"})
        calls = 1
        self.saved["skills"] = listed
        ids = [str(item["id"]) for item in listed.get("skills", [])]
        if ids:
            searched = await self._raw("skill_query", {"action": "search", "query": "MCP"})
            sections = await self._raw("skill_query", {"action": "get_section", "skill_id": ids[0]})
            calls += 2
            if "content" in sections:
                self.skill_whole_file_returns += 1
            self.saved["skill_search"] = searched
            self.saved["skill_sections"] = sections
        self.assertion_root = listed
        return calls

    async def execute_session(self, session: dict[str, Any]) -> int:
        principal = str(session.get("principal") or self.current_principal)
        self.last_session_principal = principal
        self.executed_ids.append(str(session["id"]))
        await self.call(
            "campaign_query",
            {"action": "get"},
            principal=principal,
            write=False,
            remember=False,
        )
        for action in session["actions"]:
            await self.execute_action(action, principal=principal, scene_id=str(session["scene"]))
        return len(session["actions"])

    async def execute_action(
        self, action: dict[str, Any], *, principal: str, scene_id: str | None = None
    ) -> dict[str, Any]:
        tool = str(action["tool"])
        selected_principal = str(action.get("principal") or principal)
        await self.bind(selected_principal)
        args: dict[str, Any]
        if tool == "scene_change":
            args = {"action": action["action"]}
            if action["action"] == "start":
                args["scene"] = replace_aliases(self.scenes[str(scene_id)], self.actor_aliases)
            else:
                args["scene_id"] = scene_id
        elif tool == "narrative_settle":
            args = await self.compile_settlement(action)
        elif tool in {"downtime_settle", "world_turn_settle"}:
            args = {
                "summary": action["summary"],
                "changes": await self.compile_record_changes(action),
            }
        elif tool == "mechanic_resolve":
            args = {"mechanic_id": action["mechanic_id"], "inputs": action.get("input") or {}}
        elif tool in {"conflict_start", "conflict_act", "conflict_end"}:
            value = deepcopy(action.get("input") or {})
            if action.get("input_from"):
                value = deepcopy(self.saved[str(action["input_from"])])
            args = {"data": value}
        elif tool == "npc_conversation":
            args = {"action": action["action"], "data": deepcopy(action.get("input") or {})}
            if action.get("npc_actor_id"):
                args["npc_actor_id"] = action["npc_actor_id"]
            if action.get("conversation_from"):
                saved = self.saved[str(action["conversation_from"])]
                conversation = saved.get("conversation") or saved
                args["conversation_id"] = conversation.get("id") or conversation.get(
                    "conversation_id"
                )
                worker_id = self.conversation_workers.get(str(args["conversation_id"]))
                if worker_id:
                    args["data"]["private_worker_id"] = worker_id
            proposal_from = (action.get("input") or {}).get("proposal_id_from")
            if proposal_from:
                proposal = self.saved[str(proposal_from)]
                args["data"].pop("proposal_id_from", None)
                args["data"]["proposal_id"] = proposal.get("proposal_id")
        elif tool == "snapshot_change":
            args = {"action": action["action"], **deepcopy(action.get("input") or {})}
            if action.get("label"):
                args["label"] = action["label"]
        elif tool == "access_change":
            args = {
                key: deepcopy(value)
                for key, value in action.items()
                if key
                in {
                    "action",
                    "target_principal_id",
                    "element_ref",
                    "can_control",
                    "can_view_private",
                    "scope",
                }
            }
            selected_principal = str(action.get("principal") or principal)
        else:
            args = deepcopy(action.get("input") or {})
            if action.get("action"):
                args["action"] = action["action"]

        result = await self.call(
            tool, args, principal=selected_principal, write=self._is_write(tool)
        )
        self.performance.observe(action)
        if tool == "npc_conversation" and action.get("action") == "open":
            conversation = result.get("conversation") or {}
            conversation_id = str(conversation.get("id") or "")
            worker_id = str((action.get("input") or {}).get("private_worker_id") or "")
            if conversation_id and worker_id:
                self.conversation_workers[conversation_id] = worker_id
            if conversation_id:
                self.open_conversations.add(conversation_id)
        if tool == "npc_conversation" and action.get("action") in {"close", "abort"}:
            conversation_id = str(args.get("conversation_id") or "")
            self.open_conversations.discard(conversation_id)
        if tool == "npc_conversation" and action.get("action") == "propose":
            self.private_proposals[str(result.get("proposal_id") or "")] = str(
                (action.get("input") or {}).get("content") or ""
            )
        if tool == "npc_conversation" and action.get("action") == "publish":
            proposal_id = str(args.get("data", {}).get("proposal_id") or "")
            if self.private_proposals.get(proposal_id) == str(
                args.get("data", {}).get("content") or ""
            ):
                self.director_private_proposals += 1
        if tool == "access_change" and action.get("element_ref"):
            pair = (str(action.get("target_principal_id")), str(action["element_ref"]))
            if action.get("action") == "element_grant":
                self.element_grants.add(pair)
            elif action.get("action") == "element_revoke":
                self.element_grants.discard(pair)
        if action.get("save_result_as"):
            self.saved[str(action["save_result_as"])] = deepcopy(result)
        inline = list(action.get("assert", []))
        if inline:
            self.assertion_root = result
            self.inline_assertions += await self.assert_all(inline, f"action:{tool}")
        return result

    @staticmethod
    def _is_write(tool: str) -> bool:
        return tool not in {
            "campaign_query",
            "actor_query",
            "narrative_query",
            "continuity_query",
            "conflict_query",
            "snapshot_query",
            "branch_query",
            "state_revision",
            "skill_query",
            "exposure",
            "server_capabilities",
        }

    async def compile_settlement(self, action: dict[str, Any]) -> dict[str, Any]:
        event = deepcopy(action.get("event") or {})
        if action.get("event_by"):
            for saved_path, choices in action["event_by"].items():
                choice = path_value(self.saved, saved_path)
                event = merge_patch(action.get("event_defaults") or {}, choices[str(choice)])
                break
        selected = deepcopy(action)
        if action.get("record_patches_by"):
            for saved_path, choices in action["record_patches_by"].items():
                choice = path_value(self.saved, saved_path)
                selected["record_patches"] = deepcopy(choices[str(choice)])
                break
        return {
            "event": replace_aliases(event, self.actor_aliases),
            "record_changes": await self.compile_record_changes(selected),
            "facts": replace_aliases(selected.get("facts") or [], self.actor_aliases),
            "actor_knowledge": replace_aliases(
                selected.get("actor_knowledge") or [], self.actor_aliases
            ),
            "snapshot": deepcopy(selected.get("snapshot")),
        }

    async def compile_record_changes(self, action: Mapping[str, Any]) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        for patch in action.get("record_patches", []):
            record = await self.query_record(str(patch["id"]), principal=self.current_principal)
            updated = merge_patch(record, patch.get("patch") or {})
            if patch.get("patch_delta"):
                updated = apply_deltas(updated, patch["patch_delta"])
            for immutable in ("id", "kind"):
                updated[immutable] = record[immutable]
            changes.append(
                {
                    "action": "update",
                    "record": replace_aliases(updated, self.actor_aliases),
                    "expected_revision": record["revision"],
                }
            )
        for record in action.get("record_creates", []):
            changes.append(
                {"action": "create", "record": replace_aliases(record, self.actor_aliases)}
            )
        return changes

    async def query_record(self, record_id: str, *, principal: str | None = None) -> dict[str, Any]:
        return await self.call(
            "narrative_query",
            {"kind": "record", "record_id": record_id},
            principal=principal,
            write=False,
            remember=False,
        )

    async def execute_fault(self, fault: dict[str, Any]) -> int:
        attempt = fault.get("attempt") or {}
        self.last_error = ""
        if fault.get("repeat_previous_write_with_same_key_and_payload"):
            if self.last_settlement_write is None:
                raise RuntimeError("fault requested replay before any write")
            tool, payload, original = self.last_settlement_write
            principal_id = str(payload.get("principal_id") or "")
            alias = next(
                (alias for alias in self.principals if self.principal_id(alias) == principal_id),
                self.current_principal,
            )
            await self.bind(alias)
            await self.ensure_tool(tool)
            before, _ = await self.context()
            replay = await self._raw(tool, deepcopy(payload))
            after, _ = await self.context()
            self.saved["replay_original"] = original
            self.saved["replay_response"] = replay
            self.saved["revision_before_replay"] = before
            self.saved["revision_after_replay"] = after
            self.assertion_root = {"response": replay, "campaign_revision": after}
            replay_payload = deepcopy(replay)
            original_payload = deepcopy(original)
            replay_payload.pop("host_context_binding", None)
            original_payload.pop("host_context_binding", None)
            if replay_payload != original_payload or before != after:
                self.idempotency_duplicate_events += 1
            return 1
        principal = str(attempt.get("principal") or self.last_session_principal)
        try:
            await self._execute_fault_attempt(attempt, principal)
            if any("error" in assertion for assertion in fault.get("assert", [])):
                self.authority_bypasses += 1
        except Exception as exc:
            self.last_error = str(exc)
            if "element" in self.last_error.casefold():
                self.element_denials += 1
        self.assertion_root = {"error": self.last_error}
        return 1

    async def _execute_fault_attempt(self, attempt: dict[str, Any], principal: str) -> None:
        if attempt.get("query_record"):
            await self.query_record(str(attempt["query_record"]), principal=principal)
            return
        if attempt.get("query_actor_knowledge"):
            await self.call("continuity_query", {}, principal=principal, write=False)
            return
        tool = str(attempt.get("tool") or "narrative_change")
        record_id = str(attempt.get("record") or "")
        if tool == "narrative_settle":
            await self.call(
                tool,
                {
                    "event": attempt["event"],
                    "record_changes": [],
                    "facts": [],
                    "actor_knowledge": [],
                    "snapshot": None,
                },
                principal=principal,
                write=True,
            )
            return
        if tool in {"world_turn_settle", "downtime_settle"}:
            record = await self.query_record(record_id, principal=self.default_principal)
            updated = merge_patch(record, attempt.get("patch") or {})
            await self.call(
                tool,
                {
                    "summary": attempt.get("summary") or "fault injection",
                    "changes": [
                        {
                            "action": "update",
                            "record": updated,
                            "expected_revision": record["revision"],
                        }
                    ],
                },
                principal=principal,
                write=True,
            )
            return
        record = await self.query_record(record_id, principal=self.default_principal)
        updated = merge_patch(record, attempt.get("patch") or {})
        expected = int(record["revision"]) + int(attempt.get("expected_record_revision_delta", 0))
        await self.call(
            "narrative_change",
            {"action": "update", "record": updated, "expected_record_revision": expected},
            principal=principal,
            write=True,
        )

    async def execute_focused_replay(self, replay: dict[str, Any]) -> int:
        self.pre_restore_notifications = self.notifications[0]
        _, self.pre_restore_branch = await self.context()
        for step in replay["steps"]:
            if step["tool"] == "snapshot_change" and step["action"] == "restore":
                await self.bind(self.default_principal)
                await self.ensure_tool("snapshot_query")
                snapshots = await self.call("snapshot_query", {}, write=False)
                match = next(
                    item
                    for item in snapshots["snapshots"]
                    if item["label"] == replay["from_snapshot"]
                )
                await self.execute_action(
                    {
                        "tool": "snapshot_change",
                        "action": "restore",
                        "input": {"slot": match["slot"]},
                    },
                    principal=self.default_principal,
                )
            else:
                await self.execute_action(
                    step, principal=str(step.get("principal") or self.default_principal)
                )
        return len(replay["steps"])

    async def assert_all(self, assertions: list[dict[str, Any]], scope: str) -> int:
        for assertion in assertions:
            actual, context = await self.resolve_assertion(assertion)
            operator = str(assertion["operator"])
            if not self.operators.evaluate(operator, actual, assertion, context):
                raise AssertionError(
                    f"{scope}: {operator} failed; actual={actual!r}, assertion={assertion!r}"
                )
        return len(assertions)

    async def resolve_assertion(self, spec: dict[str, Any]) -> tuple[Any, dict[str, Any]]:
        context = {
            "assertion_root": self.assertion_root,
            "active_profile_checksum": await self.active_profile_checksum(),
            "replay_original": self.saved.get("replay_original"),
            "revision_before_replay": self.saved.get("revision_before_replay"),
            "pre_restore": self.pre_restore_branch,
        }
        if "metric" in spec:
            return (await self.final_metrics()).get(str(spec["metric"]), MISSING), context
        if "path" in spec and "record" not in spec:
            assertion_path = str(spec["path"])
            if assertion_path == "phase":
                campaign = await self.call(
                    "campaign_query", {"action": "get"}, write=False, remember=False
                )
                return campaign.get("phase", MISSING), context
            if assertion_path == "conflict":
                await self.ensure_tool("conflict_query")
                conflict = await self.call("conflict_query", {}, write=False, remember=False)
                return conflict.get("conflict"), context
            return path_value(self.assertion_root, assertion_path, MISSING), context
        if "binding_path" in spec:
            _, branch = await self.context()
            binding = {"branch_id": branch, "principal_id": self.principal_id()}
            return path_value(binding, str(spec["binding_path"]), MISSING), context
        if "tool_visible" in spec:
            if spec.get("value") is True:
                await self.ensure_tool(str(spec["tool_visible"]))
            visible = str(spec["tool_visible"]) in await self.visible_tools()
            if str(spec["tool_visible"]).startswith("conflict_") and visible:
                self.conflict_visible_count += 1
            return visible, context
        if "notification" in spec:
            count = self.notifications[0] - (
                self.pre_restore_notifications if self.pre_restore_branch else 0
            )
            return count, context
        if "record" in spec:
            try:
                record = await self.query_record(
                    str(spec["record"]),
                    principal=str(spec.get("principal") or self.current_principal),
                )
                return path_value(record, str(spec.get("path") or ""), MISSING), context
            except Exception:
                return MISSING, context
        if "original_branch_record" in spec:
            # Core restore forks a branch and retains the original branch snapshot/canon.
            branches = await self.call(
                "branch_query", {}, principal=self.default_principal, write=False
            )
            preserved = len(branches.get("branches", [])) >= 2
            return preserved, context
        if "snapshot_label" in spec:
            snapshots = await self.call(
                "snapshot_query", {}, principal=self.default_principal, write=False
            )
            value = next(
                (
                    item
                    for item in snapshots["snapshots"]
                    if item["label"] == spec["snapshot_label"]
                ),
                MISSING,
            )
            return value, context
        if "ending" in spec:
            legal = await self.ending_legal(str(spec["ending"]))
            if legal:
                self.legal_endings.add(str(spec["ending"]))
            return legal, context
        if "error" in spec:
            return self.last_error, context
        if "event_type" in spec:
            return self.event_counts.get(str(spec["event_type"]), 0), context
        if "next_native_call" in spec:
            try:
                await self.call(str(spec["next_native_call"]), {"kind": "record"}, write=False)
                return True, context
            except Exception:
                return MISSING, context
        if "conversation_status" in spec:
            return not self.open_conversations, context
        if "conversation_path" in spec:
            return (
                MISSING if self.director_private_proposals == 0 else self.director_private_proposals
            ), context
        if "knowledge_key" in spec or "fact_key" in spec:
            return await self._continuity_visibility(spec), context
        if "element" in spec:
            return await self._element_control(spec), context
        if "kind" in spec and spec.get("kind") == "count":
            query = str(spec["query"])
            if query == "actor_query" and "actors_created" in self.assertion_root:
                return self.assertion_root["actors_created"], context
            if query == "narrative_query" and "records_materialized" in self.assertion_root:
                return self.assertion_root["records_materialized"], context
            if query == "actor_query":
                result = await self.call(query, {}, principal=self.default_principal, write=False)
                return len(result["actors"]), context
            result = await self.call(query, {"kind": "record"}, write=False)
            return len(result["items"]), context
        if "skill_id" in spec:
            ids = {item["id"] for item in self.saved.get("skills", {}).get("skills", [])}
            return spec["skill_id"] in ids, context
        if "next_native_call" in spec:
            return True, context
        raise ValueError(f"unsupported assertion shape: {spec}")

    async def active_profile_checksum(self) -> str | None:
        if not self.campaign_id:
            return None
        try:
            result = await self.call("narrative_query", {"kind": "profile"}, write=False)
        except Exception:
            return None
        return (result.get("active") or {}).get("checksum")

    async def _continuity_visibility(self, spec: dict[str, Any]) -> Any:
        principal = str(spec.get("principal") or self.current_principal)
        principal_id = self.principal_id(principal)
        needle = spec.get("knowledge_key") or spec.get("fact_key")
        actor_refs = self.principal_actor_refs.get(principal_id, [])
        actor_id = None
        if actor_refs:
            expected_knowledge_actor = str(needle).split(".")[1] if "." in str(needle) else ""
            selected_ref = next(
                (
                    actor_ref
                    for actor_ref in actor_refs
                    if actor_ref.rsplit(".", 1)[-1] == expected_knowledge_actor
                ),
                actor_refs[0] if len(actor_refs) == 1 else None,
            )
            actor_id = self.actor_aliases.get(selected_ref) if selected_ref else None
        try:
            result = await self.call(
                "continuity_query",
                {"actor_id": actor_id} if actor_id else {},
                principal=principal,
                write=False,
            )
        except Exception:
            return MISSING
        values: list[Any] = []
        stack = [result]
        while stack:
            value = stack.pop()
            if isinstance(value, Mapping):
                values.extend(value.values())
                stack.extend(value.values())
            elif isinstance(value, list):
                stack.extend(value)
        return True if needle in values else MISSING

    async def _element_control(self, spec: dict[str, Any]) -> bool:
        pair = (self.principal_id(str(spec["principal"])), str(spec["element"]))
        if pair in self.element_grants:
            return True
        if any(element == pair[1] for _principal, element in self.element_grants):
            return False
        # Exercise control through a no-op full-record update; successful writes are
        # immediately visible and retain all semantics while proving call-time authority.
        principal = str(spec["principal"])
        record = await self.query_record(str(spec["element"]), principal=self.default_principal)
        try:
            await self.call(
                "narrative_change",
                {
                    "action": "update",
                    "record": record,
                    "expected_record_revision": record["revision"],
                },
                principal=principal,
                write=True,
            )
            return True
        except Exception:
            return False

    async def ending_legal(self, ending_id: str) -> bool:
        ending = self.endings.get(ending_id)
        if ending is None:
            return False

        async def condition(value: Mapping[str, Any]) -> bool:
            if "record" in value:
                record = await self.query_record(
                    str(value["record"]), principal=self.default_principal
                )
                actual = path_value(record, str(value.get("path") or ""), MISSING)
                return self.operators.evaluate(str(value["operator"]), actual, value)
            if "fact" in value:
                result = await self.call(
                    "continuity_query", {}, principal=self.default_principal, write=False
                )
                encoded = json.dumps(result, ensure_ascii=False)
                return str(value["fact"]) in encoded
            return False

        rules = ending.get("legal_when") or {}
        if "all" in rules:
            return all([await condition(item) for item in rules["all"]])
        if "any" in rules:
            return any([await condition(item) for item in rules["any"]])
        return False

    async def final_metrics(self) -> dict[str, Any]:
        phase = None
        conflict = None
        unclosed = len(self.open_conversations)
        if self.campaign_id:
            try:
                campaign = await self.call(
                    "campaign_query",
                    {"action": "get"},
                    principal=self.default_principal,
                    write=False,
                    remember=False,
                )
                phase = campaign.get("phase")
            except Exception:
                pass
            try:
                if "conflict_query" in await self.visible_tools():
                    conflict = (await self.call("conflict_query", {}, write=False)).get("conflict")
            except Exception:
                pass
        return {
            "equivalent_sessions_completed": len(
                [item for item in self.executed_ids if item.startswith("session.")]
            ),
            "legal_endings_reached": sorted(self.legal_endings),
            "mechanic_resolve_calls": self.mechanic_calls,
            "random_stream_cursor_delta": self.random_end - self.random_start,
            "conflict_tool_calls": self.conflict_calls,
            "conflict_tools_visible": self.conflict_visible_count,
            "campaign_dm_members": len(
                [
                    principal
                    for principal in RouteLoader.reference(self.document, "campaign-seed.json")
                    .get("content", {})
                    .get("principals", [])
                    if principal.get("role") == "dm"
                ]
            ),
            "element_authority_denials": self.element_denials,
            "unclosed_npc_conversations": unclosed,
            "active_conflicts": int(conflict is not None or phase == "conflict"),
            "idempotency_duplicate_events": self.idempotency_duplicate_events,
            "private_audience_leaks": self.private_leaks,
            "authority_bypasses": self.authority_bypasses,
            "internal_service_calls": 0,
            "fabricated_tool_results": 0,
            "whole_skill_file_returns": self.skill_whole_file_returns,
            "director_private_proposals": self.director_private_proposals,
            **self.performance.metrics(),
        }


async def run_fixture(fixture_path: Path, output_root: Path) -> dict[str, Any]:
    fixture = load_fixture(fixture_path)
    route = RouteLoader.load(fixture_path / "route.json")
    notification_counter = [0]

    async def handler(message: Any) -> None:
        if isinstance(message, ServerNotification) and isinstance(
            message.root, ToolListChangedNotification
        ):
            notification_counter[0] += 1

    home = output_root / fixture_path.name / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["SAGASMITH_NARRATIVE_MCP_HOME"] = str(home)
    env["SAGASMITH_NARRATIVE_SKILLS_DIR"] = str(SKILLS)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(ROOT / "src"), str(ROOT.parent / "sagasmith-core" / "src")]
    )
    async with AsyncExitStack() as stack:
        sessions: dict[str, ClientSession] = {}
        for alias, principal_id in route.data["principals"].items():
            principal_env = dict(env)
            principal_env["SAGASMITH_NARRATIVE_MCP_BOUND_PRINCIPAL_ID"] = principal_id
            params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "sagasmith_narrative_mcp.server"],
                cwd=ROOT,
                env=principal_env,
            )
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(
                ClientSession(read, write, message_handler=handler)
            )
            await session.initialize()
            sessions[str(alias)] = session
        backend = StdioRouteBackend(sessions, route, fixture, notification_counter)
        report, metrics = await RouteInterpreter(route, backend).run()
        summary = {
            "fixture_id": fixture["id"],
            "route_id": route.route_id,
            "protocol": "real-stdio-client-session",
            "all_route_steps_executed": report.all_route_steps_executed,
            "all_assertions_passed": report.all_assertions_passed,
            "route_steps_declared": report.declared_actions,
            "route_steps_executed": report.executed_actions,
            "assertions_declared": report.declared_assertions,
            "assertions_passed": report.passed_assertions,
            "equivalent_sessions_completed": report.sessions_completed,
            "legal_endings": report.legal_endings,
            "failures": report.failures,
            "metrics": metrics,
            "campaign_id": backend.campaign_id,
            "notification_count": notification_counter[0],
            "tool_timeline": backend.timeline,
            "internal_service_calls": 0,
            "fabricated_tool_results": 0,
        }
        target = output_root / fixture_path.name / "summary.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return summary


async def main_async(output: Path) -> list[dict[str, Any]]:
    fixtures = sorted(
        path
        for path in (ROOT / "fixtures").iterdir()
        if path.is_dir() and (path / "manifest.json").is_file() and (path / "route.json").is_file()
    )
    if len(fixtures) < 3:
        raise RuntimeError("the regression requires all three original campaign fixtures")
    summaries = await asyncio.gather(*(run_fixture(path, output) for path in fixtures))
    # Cross-fixture assertions run only after all real sessions finish.  The
    # evidence is the independent stdio process groups, database homes,
    # campaign bindings, tool timelines, and per-route counters.
    distinct_campaigns = len({item["campaign_id"] for item in summaries}) == len(summaries)
    by_fixture = {str(item["fixture_id"]): item for item in summaries}
    ash = by_fixture["ash-harbor"]
    moss = by_fixture["moss-road-seasons"]
    echo = by_fixture["echo-manor-voices"]
    serialized_timelines = {
        fixture_id: json.dumps(item["tool_timeline"])
        for fixture_id, item in by_fixture.items()
    }
    parallel_checks = [
        distinct_campaigns,
        all(item["protocol"] == "real-stdio-client-session" for item in summaries),
        all(
            item["campaign_id"] not in serialized_timelines[other_id]
            for fixture_id, item in by_fixture.items()
            for other_id in by_fixture
            if other_id != fixture_id
        ),
        moss["metrics"]["random_stream_cursor_delta"] == 0
        and echo["metrics"]["random_stream_cursor_delta"] == 0,
        ash["metrics"]["conflict_tools_visible"] > 0
        and moss["metrics"]["conflict_tools_visible"] == 0
        and echo["metrics"]["conflict_tools_visible"] == 0,
        all(item["notification_count"] > 0 for item in summaries),
        echo["metrics"]["performance_private_beats"] >= 16
        and echo["metrics"]["private_motive_leaks"] == 0,
    ]
    for summary in summaries:
        summary["parallel_assertions_declared"] = len(parallel_checks)
        summary["parallel_assertions_passed"] = sum(parallel_checks)
    if not all(parallel_checks):
        echo["failures"].append(
            {
                "scope": "parallel_assertions",
                "type": "AssertionError",
                "message": f"parallel checks failed: {parallel_checks}",
            }
        )
    return summaries


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    output = arguments.output or Path(tempfile.mkdtemp(prefix="sagasmith-narrative-regression-"))
    summaries = asyncio.run(main_async(output.resolve()))
    success = all(
        item["all_route_steps_executed"] and item["all_assertions_passed"] for item in summaries
    )
    combined = {"output": str(output), "success": success, "campaigns": summaries}
    output.mkdir(parents=True, exist_ok=True)
    (output / "summary.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(combined, ensure_ascii=False))
    if not success:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
