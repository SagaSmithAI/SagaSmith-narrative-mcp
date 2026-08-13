from __future__ import annotations

from pathlib import Path

import pytest
from sagasmith_core.database import Database, sqlite_database_url

from sagasmith_narrative_mcp.config import McpConfig
from sagasmith_narrative_mcp.contracts import (
    narrative_document,
    validate_profile,
    validate_record,
)
from sagasmith_narrative_mcp.runtime import NarrativeRuntime
from sagasmith_narrative_mcp.server import create_server


def runtime(path: Path) -> NarrativeRuntime:
    database = Database(sqlite_database_url(path))
    database.create_schema()
    return NarrativeRuntime(database)


def state(rt: NarrativeRuntime, campaign_id: str) -> tuple[int, str]:
    return rt.campaigns.get(campaign_id).revision, rt.branch_id(campaign_id)


def campaign(rt: NarrativeRuntime) -> str:
    return rt.campaign_create(
        name="Full hardening", principal_id="owner", idempotency_key="campaign-create"
    )["id"]


def activate_profile(
    rt: NarrativeRuntime,
    campaign_id: str,
    *,
    facilitator_roles: list[str] | None = None,
    capabilities: list[str] | None = None,
) -> None:
    revision, branch_id = state(rt, campaign_id)
    profile = {
        "id": "profile.hardening",
        "version": "1.0.0",
        "mechanics_level": 0,
        "capabilities": capabilities or [],
        "authority": {
            "facilitator_roles": (
                ["owner", "dm"] if facilitator_roles is None else facilitator_roles
            ),
            "audience_scopes": ["table", "public", "actor", "facilitator"],
        },
        "sources": [{"type": "self-authored", "citation": __file__}],
    }
    rt.profile_change(
        campaign_id,
        action="create_draft",
        profile=profile,
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="profile-draft",
    )
    revision, branch_id = state(rt, campaign_id)
    rt.profile_change(
        campaign_id,
        action="finalize",
        profile_key="profile.hardening@1.0.0",
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="profile-finalize",
    )
    revision, branch_id = state(rt, campaign_id)
    rt.profile_change(
        campaign_id,
        action="activate",
        profile_key="profile.hardening@1.0.0",
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="profile-activate",
    )


def enter_play(rt: NarrativeRuntime, campaign_id: str) -> None:
    revision, branch_id = state(rt, campaign_id)
    rt.set_phase(
        campaign_id,
        phase="play",
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="enter-play",
    )


def test_finalization_and_audience_shapes_fail_closed() -> None:
    with pytest.raises(ValueError, match="evidence locator"):
        validate_profile(
            {
                "id": "profile.bad-source",
                "version": "1",
                "mechanics_level": 0,
                "sources": [{"type": "claimed"}],
            },
            finalized=True,
        )
    with pytest.raises(ValueError, match="actor_schema"):
        validate_profile(
            {
                "id": "profile.bad-schema",
                "version": "1",
                "mechanics_level": 0,
                "actor_schema": {"type": "definitely-not-json-schema"},
                "sources": [{"type": "original", "ref": "test"}],
            }
        )
    with pytest.raises(ValueError, match="private_worker"):
        validate_record(
            {
                "id": "secret.worker",
                "kind": "secret",
                "audience": {"scope": "private_worker", "worker_id": "worker.only"},
            }
        )


def test_facilitatorless_owner_has_no_implicit_actor_authority_and_actor_can_update(
    tmp_path: Path,
) -> None:
    rt = runtime(tmp_path / "actor-authority.db")
    campaign_id = campaign(rt)
    activate_profile(rt, campaign_id, facilitator_roles=[])
    actor = rt.actor_create(
        campaign_id,
        principal_id="owner",
        actor={"name": "Private NPC", "type": "npc", "sheet": {"motive": "leave"}},
        expected_revision=state(rt, campaign_id)[0],
        expected_branch_id=state(rt, campaign_id)[1],
        idempotency_key="actor-create",
    )
    with pytest.raises(PermissionError):
        rt.actor_query(campaign_id, principal_id="owner", actor_id=actor["id"])

    revision, branch_id = state(rt, campaign_id)
    rt.access_change(
        campaign_id,
        action="actor_grant",
        target_principal_id="owner",
        actor_id=actor["id"],
        can_control=True,
        can_view_private=True,
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="owner-actor-grant",
    )
    current = rt.actor_query(campaign_id, principal_id="owner", actor_id=actor["id"])
    revision, branch_id = state(rt, campaign_id)
    arguments = dict(
        principal_id="owner",
        actor_id=actor["id"],
        actor={"sheet": {"motive": "stay", "arc": "committed"}},
        expected_actor_revision=current["revision"],
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="actor-update",
    )
    first = rt.actor_update(campaign_id, **arguments)
    assert rt.actor_update(campaign_id, **arguments) == first
    assert first["sheet"]["arc"] == "committed"


def test_access_revocation_is_atomic_and_preserves_the_last_owner(tmp_path: Path) -> None:
    rt = runtime(tmp_path / "access-revoke.db")
    campaign_id = campaign(rt)
    actor = rt.actor_create(
        campaign_id,
        principal_id="owner",
        actor={"name": "Revocable actor", "type": "pc"},
        expected_revision=state(rt, campaign_id)[0],
        expected_branch_id=state(rt, campaign_id)[1],
        idempotency_key="revocable-actor-create",
    )
    revision, branch_id = state(rt, campaign_id)
    rt.access_change(
        campaign_id,
        action="campaign_grant",
        target_principal_id="player",
        role="player",
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="player-campaign-grant",
    )
    revision, branch_id = state(rt, campaign_id)
    rt.access_change(
        campaign_id,
        action="actor_grant",
        target_principal_id="player",
        actor_id=actor["id"],
        can_control=True,
        can_view_private=True,
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="player-actor-grant",
    )
    assert rt.actor_query(campaign_id, principal_id="player", actor_id=actor["id"])["id"]

    revision, branch_id = state(rt, campaign_id)
    rt.access_change(
        campaign_id,
        action="actor_revoke",
        target_principal_id="player",
        actor_id=actor["id"],
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="player-actor-revoke",
    )
    with pytest.raises(PermissionError):
        rt.actor_query(campaign_id, principal_id="player", actor_id=actor["id"])

    revision, branch_id = state(rt, campaign_id)
    rt.access_change(
        campaign_id,
        action="campaign_revoke",
        target_principal_id="player",
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="player-campaign-revoke",
    )
    with pytest.raises(PermissionError):
        rt.campaign_get(campaign_id, "player")

    revision, branch_id = state(rt, campaign_id)
    with pytest.raises(ValueError, match="last owner"):
        rt.access_change(
            campaign_id,
            action="campaign_revoke",
            target_principal_id="owner",
            principal_id="owner",
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key="last-owner-revoke",
        )
    assert state(rt, campaign_id) == (revision, branch_id)


def test_campaign_seed_materializes_element_grants(tmp_path: Path) -> None:
    rt = runtime(tmp_path / "seed-grants.db")
    campaign_id = campaign(rt)
    activate_profile(rt, campaign_id, facilitator_roles=[])
    pack = {
        "id": "pack.seed-grants",
        "version": "1",
        "title": "Seed grants",
        "kind": "campaign_seed",
        "sources": [{"type": "self-authored", "citation": __file__}],
        "rights": {"distribution": "public", "license": "Apache-2.0"},
        "review": {"agent_finalization": True},
        "content": {
            "principals": [
                {
                    "id": "player.seed",
                    "role": "player",
                    "actor_grants": ["actor.seed"],
                    "element_grants": ["location.seed", "actor.seed"],
                }
            ],
            "actors": [{"id": "actor.seed", "name": "Seed actor", "type": "pc"}],
            "records": [
                {
                    "id": "location.seed",
                    "kind": "location",
                    "audience": {"scope": "table"},
                    "controller": {"scope": "steward", "principal_id": "player.seed"},
                }
            ],
        },
    }
    revision, branch_id = state(rt, campaign_id)
    rt.pack_change(
        campaign_id,
        action="create_draft",
        pack=pack,
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="seed-draft",
    )
    for action, key in (
        ("finalize", "seed-finalize"),
        ("import", "seed-import"),
        ("activate", "seed-activate"),
    ):
        revision, branch_id = state(rt, campaign_id)
        rt.pack_change(
            campaign_id,
            action=action,
            pack_key="pack.seed-grants@1",
            principal_id="owner",
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key=key,
        )
    revision, branch_id = state(rt, campaign_id)
    result = rt.campaign_seed_apply(
        campaign_id,
        pack_key="pack.seed-grants@1",
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="seed-apply",
    )
    assert result["element_grants_materialized"] == 2
    document = narrative_document(rt.campaigns.get(campaign_id).state)
    assert document["element_grants"][0]["element_ref"] == "location.seed"
    authoritative_actor_id = document["actor_bindings"]["actor.seed"]
    enter_play(rt, campaign_id)
    revision, branch_id = state(rt, campaign_id)
    settled = rt.narrative_settle(
        campaign_id,
        event={
            "event_type": "seed.actor-fact",
            "summary": "The logical actor disclosed an objective fact.",
            "audience_scope": "public",
        },
        facts=[
            {
                "action": "upsert",
                "fact_key": "seed.actor-fact",
                "content": "The actor spoke.",
                "kind": "public_record",
                "subject_ref": authoritative_actor_id,
                "predicate": "spoke",
                "disclosure_scope": "public",
            }
        ],
        principal_id="player.seed",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="seed-actor-fact",
    )
    assert settled["event"]["event_type"] == "seed.actor-fact"


def test_scene_authority_and_npc_private_proposal_recovery(tmp_path: Path) -> None:
    rt = runtime(tmp_path / "scene-npc.db")
    campaign_id = campaign(rt)
    activate_profile(rt, campaign_id, capabilities=["npc_conversation"])
    rt.access.ensure_principal("player")
    rt.access.grant_campaign(campaign_id, "player", role="player")
    npc = rt.actor_create(
        campaign_id,
        principal_id="owner",
        actor={"name": "Bound NPC", "type": "npc"},
        expected_revision=state(rt, campaign_id)[0],
        expected_branch_id=state(rt, campaign_id)[1],
        idempotency_key="npc-create",
    )
    enter_play(rt, campaign_id)
    revision, branch_id = state(rt, campaign_id)
    with pytest.raises(PermissionError, match="scene start"):
        rt.scene_change(
            campaign_id,
            action="start",
            scene={"id": "scene.denied"},
            principal_id="player",
            expected_revision=revision,
            expected_branch_id=branch_id,
            idempotency_key="scene-denied",
        )
    opened = rt.npc_conversation(
        campaign_id,
        action="open",
        npc_actor_id=npc["id"],
        data={"private_worker_id": "worker.bound"},
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="npc-open",
    )
    conversation_id = opened["conversation"]["id"]
    revision, branch_id = state(rt, campaign_id)
    proposal = rt.npc_conversation(
        campaign_id,
        action="propose",
        conversation_id=conversation_id,
        data={"private_worker_id": "worker.bound", "content": "private motive"},
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="npc-propose",
    )
    revision, branch_id = state(rt, campaign_id)
    closed = rt.npc_conversation(
        campaign_id,
        action="close",
        conversation_id=conversation_id,
        data={
            "private_worker_id": "worker.bound",
            "selected_proposal_ids": [proposal["proposal_id"]],
            "settlement": {
                "event": {
                    "event_type": "npc.accepted",
                    "summary": "A public consequence was selected.",
                    "audience_scope": "public",
                }
            },
        },
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="npc-close",
    )
    receipt = closed["npc_conversation"]
    assert receipt["accepted_proposal_ids"] == [proposal["proposal_id"]]
    assert "accepted_proposals" not in receipt
    assert "private motive" not in str(closed)


def test_snapshot_and_branch_changes_are_admin_cas_and_exactly_replayable(
    tmp_path: Path,
) -> None:
    server = create_server(McpConfig(database_url=sqlite_database_url(tmp_path / "recovery.db")))
    rt = server.runtime
    campaign_id = campaign(rt)
    rt.access.ensure_principal("player")
    rt.access.grant_campaign(campaign_id, "player", role="player")
    snapshot_tool = server._tool_manager.get_tool("snapshot_change")
    snapshot_query = server._tool_manager.get_tool("snapshot_query")
    branch_tool = server._tool_manager.get_tool("branch_change")
    with pytest.raises(PermissionError):
        snapshot_query.fn(campaign_id=campaign_id, principal_id="player")

    activate_profile(rt, campaign_id, capabilities=["npc_conversation"])
    npc = rt.actor_create(
        campaign_id,
        principal_id="owner",
        actor={"name": "Recovery barrier NPC", "type": "npc"},
        expected_revision=state(rt, campaign_id)[0],
        expected_branch_id=state(rt, campaign_id)[1],
        idempotency_key="recovery-npc-create",
    )
    enter_play(rt, campaign_id)
    revision, branch_id = state(rt, campaign_id)
    opened = rt.npc_conversation(
        campaign_id,
        action="open",
        npc_actor_id=npc["id"],
        data={"private_worker_id": "worker.recovery"},
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="recovery-npc-open",
    )
    revision, branch_id = state(rt, campaign_id)
    recovery_arguments = {
        "campaign_id": campaign_id,
        "expected_revision": revision,
        "expected_branch_id": branch_id,
        "principal_id": "owner",
    }
    with pytest.raises(ValueError, match="NPC conversation"):
        snapshot_tool.fn(
            action="restore",
            slot=1,
            idempotency_key="restore-while-npc-open",
            **recovery_arguments,
        )
    with pytest.raises(ValueError, match="NPC conversation"):
        branch_tool.fn(
            action="create",
            name="unsafe",
            idempotency_key="branch-while-npc-open",
            **recovery_arguments,
        )
    rt.npc_conversation(
        campaign_id,
        action="abort",
        conversation_id=opened["conversation"]["id"],
        data={"private_worker_id": "worker.recovery"},
        principal_id="owner",
        expected_revision=revision,
        expected_branch_id=branch_id,
        idempotency_key="recovery-npc-abort",
    )

    revision, branch_id = state(rt, campaign_id)
    arguments = {
        "campaign_id": campaign_id,
        "action": "create",
        "label": "CAS checkpoint",
        "expected_revision": revision,
        "expected_branch_id": branch_id,
        "idempotency_key": "snapshot-create",
        "principal_id": "owner",
    }
    first = snapshot_tool.fn(**arguments)
    assert snapshot_tool.fn(**arguments) == first
    revision, branch_id = state(rt, campaign_id)
    branch_arguments = {
        "campaign_id": campaign_id,
        "action": "create",
        "name": "alternate",
        "expected_revision": revision,
        "expected_branch_id": branch_id,
        "idempotency_key": "branch-create",
        "principal_id": "owner",
    }
    branch = branch_tool.fn(**branch_arguments)
    assert branch["campaign_revision"] == revision + 1
    assert state(rt, campaign_id)[0] == branch["campaign_revision"]
    assert branch_tool.fn(**branch_arguments) == branch
