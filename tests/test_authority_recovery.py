from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from sagasmith_core.database import Database, sqlite_database_url

from sagasmith_narrative_mcp.config import McpConfig
from sagasmith_narrative_mcp.exposure import ExposureError
from sagasmith_narrative_mcp.policies import CORE_TOOLS
from sagasmith_narrative_mcp.runtime import NarrativeRuntime
from sagasmith_narrative_mcp.server import create_server


def make_runtime(path: Path) -> NarrativeRuntime:
    database = Database(sqlite_database_url(path))
    database.create_schema()
    return NarrativeRuntime(database)


def create_campaign(runtime: NarrativeRuntime, *, key: str = "create") -> str:
    return runtime.campaign_create(
        name="Authority regression",
        principal_id="owner",
        idempotency_key=key,
    )["id"]


def state(runtime: NarrativeRuntime, campaign_id: str) -> tuple[int, str]:
    campaign = runtime.campaigns.get(campaign_id)
    return campaign.revision, runtime.branch_id(campaign_id)


def grant_member(
    runtime: NarrativeRuntime,
    campaign_id: str,
    principal_id: str,
    *,
    role: str,
) -> None:
    runtime.access.ensure_principal(principal_id, platform="test")
    runtime.access.grant_campaign(campaign_id, principal_id, role=role)


def write_profile(
    runtime: NarrativeRuntime,
    campaign_id: str,
    *,
    profile_id: str = "profile.authority",
    version: str = "1.0.0",
) -> str:
    profile_key = f"{profile_id}@{version}"
    revision, branch_id = state(runtime, campaign_id)
    runtime.profile_change(
        campaign_id,
        action="create_draft",
        profile={
            "id": profile_id,
            "version": version,
            "title": "Authority profile",
            "mechanics_level": 0,
            "capabilities": ["npc_conversation"],
            "sources": [
                {
                    "type": "self-authored",
                    "license": "Apache-2.0",
                    "citation": "tests/test_authority_recovery.py",
                }
            ],
        },
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key=f"profile-draft-{profile_id}-{version}",
    )
    revision, branch_id = state(runtime, campaign_id)
    runtime.profile_change(
        campaign_id,
        action="finalize",
        profile_key=profile_key,
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key=f"profile-finalize-{profile_id}-{version}",
    )
    return profile_key


def write_pack(
    runtime: NarrativeRuntime,
    campaign_id: str,
    *,
    pack_id: str,
    version: str = "1.0.0",
    dependencies: list[dict[str, str]] | None = None,
) -> str:
    pack_key = f"{pack_id}@{version}"
    revision, branch_id = state(runtime, campaign_id)
    runtime.pack_change(
        campaign_id,
        action="create_draft",
        pack={
            "id": pack_id,
            "version": version,
            "title": pack_id,
            "kind": "module",
            "dependencies": dependencies or [],
            "sources": [
                {
                    "type": "self-authored",
                    "license": "Apache-2.0",
                    "citation": "tests/test_authority_recovery.py",
                }
            ],
            "rights": {"distribution": "public", "license": "Apache-2.0"},
            "review": {"agent_finalization": True},
            "content": {},
        },
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key=f"pack-draft-{pack_id}-{version}",
    )
    revision, branch_id = state(runtime, campaign_id)
    runtime.pack_change(
        campaign_id,
        action="finalize",
        pack_key=pack_key,
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key=f"pack-finalize-{pack_id}-{version}",
    )
    return pack_key


def enter_play(runtime: NarrativeRuntime, campaign_id: str) -> None:
    profile_key = write_profile(runtime, campaign_id)
    revision, branch_id = state(runtime, campaign_id)
    runtime.profile_change(
        campaign_id,
        action="activate",
        profile_key=profile_key,
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="profile-activate",
    )
    revision, branch_id = state(runtime, campaign_id)
    runtime.set_phase(
        campaign_id,
        phase="play",
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="enter-play",
    )


def test_observer_cannot_inject_settlement_facts_or_knowledge(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path / "observer.db")
    campaign_id = create_campaign(runtime)
    grant_member(runtime, campaign_id, "observer", role="observer")
    actor = runtime.actor_create(
        campaign_id,
        principal_id="owner",
        idempotency_key="actor",
        actor={"name": "Witness", "type": "npc"},
    )
    revision, branch_id = state(runtime, campaign_id)

    with pytest.raises(PermissionError):
        runtime.narrative_settle(
            campaign_id,
            principal_id="observer",
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key="observer-injection",
            event={
                "event_type": "forged.event",
                "summary": "An observer must not author campaign truth.",
                "audience_scope": "public",
            },
            facts=[
                {
                    "action": "add",
                    "fact_key": "forged.fact",
                    "content": "Forged objective truth.",
                    "kind": "fact",
                    "disclosure_scope": "public",
                }
            ],
            actor_knowledge=[
                {
                    "action": "add",
                    "actor_id": actor["id"],
                    "knowledge_key": "forged.knowledge",
                    "proposition": "Forged subjective truth.",
                    "disclosure_scope": "owner",
                }
            ],
        )


def test_snapshot_restore_requires_campaign_admin(tmp_path: Path) -> None:
    database_url = sqlite_database_url(tmp_path / "snapshot.db")
    server = create_server(McpConfig(database_url=database_url))
    runtime = server.runtime
    campaign_id = create_campaign(runtime)
    grant_member(runtime, campaign_id, "player", role="player")
    snapshot = runtime.snapshots.create(campaign_id, label="admin checkpoint")
    revision, branch_id = state(runtime, campaign_id)
    tool = server._tool_manager.get_tool("snapshot_change")

    with pytest.raises(PermissionError):
        tool.fn(
            campaign_id=campaign_id,
            action="restore",
            slot=snapshot.slot,
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key="player-restore",
            principal_id="player",
        )


@pytest.mark.parametrize("kind", ["profile", "pack"])
def test_finalized_identity_cannot_be_reopened_or_overwritten(tmp_path: Path, kind: str) -> None:
    runtime = make_runtime(tmp_path / f"immutable-{kind}.db")
    campaign_id = create_campaign(runtime)
    if kind == "profile":
        write_profile(runtime, campaign_id)
        revision, branch_id = state(runtime, campaign_id)
        with pytest.raises(ValueError, match="finalized|immutable|already exists"):
            runtime.profile_change(
                campaign_id,
                action="create_draft",
                profile={
                    "id": "profile.authority",
                    "version": "1.0.0",
                    "title": "Attempted replacement",
                    "mechanics_level": 0,
                    "sources": [{"type": "self-authored"}],
                },
                principal_id="owner",
                expected_revision=revision,
                expected_branch_id=branch_id,
                idempotency_key="replace-final-profile",
            )
    else:
        write_pack(runtime, campaign_id, pack_id="pack.immutable")
        revision, branch_id = state(runtime, campaign_id)
        with pytest.raises(ValueError, match="finalized|immutable|already exists"):
            runtime.pack_change(
                campaign_id,
                action="create_draft",
                pack={
                    "id": "pack.immutable",
                    "version": "1.0.0",
                    "title": "Attempted replacement",
                    "kind": "module",
                },
                principal_id="owner",
                expected_revision=revision,
                expected_branch_id=branch_id,
                idempotency_key="replace-final-pack",
            )


def test_pack_activation_rejects_missing_dependency(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path / "dependency.db")
    campaign_id = create_campaign(runtime)
    pack_key = write_pack(
        runtime,
        campaign_id,
        pack_id="pack.dependent",
        dependencies=[{"id": "pack.missing", "version": "1.0.0"}],
    )
    revision, branch_id = state(runtime, campaign_id)
    runtime.pack_change(
        campaign_id,
        action="import",
        pack_key=pack_key,
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="import-dependent",
    )
    revision, branch_id = state(runtime, campaign_id)

    with pytest.raises(ValueError, match="depend"):
        runtime.pack_change(
            campaign_id,
            action="activate",
            pack_key=pack_key,
            principal_id="owner",
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key="activate-dependent",
        )


def test_npc_conversation_rejects_non_owner(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path / "npc.db")
    campaign_id = create_campaign(runtime)
    enter_play(runtime, campaign_id)
    grant_member(runtime, campaign_id, "player", role="player")
    npc = runtime.actor_create(
        campaign_id,
        principal_id="owner",
        idempotency_key="npc",
        actor={"name": "Isolated NPC", "type": "npc"},
    )
    revision, branch_id = state(runtime, campaign_id)
    with pytest.raises(PermissionError):
        runtime.npc_conversation(
            campaign_id,
            action="open",
            npc_actor_id=npc["id"],
            data={"private_worker_id": "worker.untrusted"},
            principal_id="player",
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key="player-opens-npc",
        )


def test_npc_conversation_rejects_stale_context(tmp_path: Path) -> None:
    runtime = make_runtime(tmp_path / "npc-stale.db")
    campaign_id = create_campaign(runtime)
    enter_play(runtime, campaign_id)
    npc = runtime.actor_create(
        campaign_id,
        principal_id="owner",
        idempotency_key="npc",
        actor={"name": "Isolated NPC", "type": "npc"},
    )
    revision, branch_id = state(runtime, campaign_id)
    opened = runtime.npc_conversation(
        campaign_id,
        action="open",
        npc_actor_id=npc["id"],
        data={"private_worker_id": "worker.trusted"},
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="owner-opens-npc",
    )
    conversation_id = opened["conversation"]["id"]
    revision, branch_id = state(runtime, campaign_id)
    mutation_was_blocked = False
    try:
        runtime.record_change(
            campaign_id,
            action="create",
            record={"id": "clock.external", "kind": "clock"},
            principal_id="owner",
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key="external-mutation",
        )
    except ValueError as error:
        assert "conversation" in str(error).lower() or "close" in str(error).lower()
        mutation_was_blocked = True

    if mutation_was_blocked:
        current = runtime.campaigns.get(campaign_id)
        runtime.campaigns.update(
            campaign_id,
            state=current.state,
            expected_revision=current.revision,
        )
        revision, branch_id = state(runtime, campaign_id)
        aborted = runtime.npc_conversation(
            campaign_id,
            action="abort",
            conversation_id=conversation_id,
            data={"private_worker_id": "worker.trusted"},
            principal_id="owner",
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key="abort-stale-conversation",
        )
        assert aborted["status"] == "aborted"
    else:
        revision, branch_id = state(runtime, campaign_id)
        with pytest.raises(ValueError, match="stale|close|abort"):
            runtime.npc_conversation(
                campaign_id,
                action="propose",
                conversation_id=conversation_id,
                data={"content": "This proposal used stale context."},
                principal_id="owner",
                expected_revision=revision,
                expected_branch_id=branch_id,
                idempotency_key="stale-proposal",
            )


def test_settlement_cas_allows_only_one_concurrent_writer(tmp_path: Path) -> None:
    database_url = sqlite_database_url(tmp_path / "cas.db")
    first = NarrativeRuntime(Database(database_url))
    first.database.create_schema()
    campaign_id = create_campaign(first)
    second = NarrativeRuntime(Database(database_url))
    revision, branch_id = state(first, campaign_id)
    start = Barrier(2)

    def settle(runtime: NarrativeRuntime, suffix: str) -> tuple[str, object]:
        start.wait(timeout=5)
        try:
            return (
                "ok",
                runtime.narrative_settle(
                    campaign_id,
                    principal_id="owner",
                    expected_revision=revision,
                    expected_branch_id=branch_id,
                    idempotency_key=f"cas-{suffix}",
                    event={
                        "event_type": f"cas.{suffix}",
                        "summary": f"Concurrent writer {suffix}",
                        "audience_scope": "public",
                    },
                ),
            )
        except Exception as error:  # asserted below with the exact failure contract
            return "error", error

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda item: settle(*item),
                [(first, "first"), (second, "second")],
            )
        )
    successes = [value for status, value in results if status == "ok"]
    failures = [value for status, value in results if status == "error"]
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ValueError)
    assert "revision conflict" in str(failures[0]).lower()
    assert first.campaigns.get(campaign_id).revision == revision + 1


def test_no_request_context_lists_only_bootstrap_and_rejects_domain_call(
    tmp_path: Path,
) -> None:
    server = create_server(McpConfig(database_url=sqlite_database_url(tmp_path / "no-context.db")))
    tools = asyncio.run(server.list_tools())
    assert {tool.name for tool in tools} == set(CORE_TOOLS)

    with pytest.raises(ExposureError):
        asyncio.run(
            server.call_tool(
                "campaign_setup",
                {
                    "action": "create",
                    "name": "Must not be created without an exposure",
                    "idempotency_key": "no-context",
                },
            )
        )
