from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest

from sagasmith_narrative_mcp.route_dsl import (
    OperatorRegistry,
    PerformanceOracle,
    RouteLoader,
    apply_deltas,
    merge_patch,
    path_value,
    replace_aliases,
)

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize(
    "fixture", ["ash-harbor", "moss-road-seasons", "echo-manor-voices"]
)
def test_checked_in_route_loads_and_counts_every_declared_node(fixture: str) -> None:
    route = RouteLoader.load(ROOT / "fixtures" / fixture / "route.json")

    assert route.route_id
    assert route.declared_action_count > len(route.data["sessions"])
    assert route.declared_assertion_count > 0


def test_merge_patch_delta_alias_and_path_helpers_are_immutable() -> None:
    original = {"id": "clock.test", "data": {"current": 1, "keep": True}}
    patched = merge_patch(original, {"data": {"keep": None, "maximum": 4}})
    changed = apply_deltas(patched, {"data.current": 2})
    aliased = replace_aliases(
        {"actor_id": "actor.one", "nested": ["actor.one"]}, {"actor.one": "uuid-1"}
    )

    assert path_value(changed, "data.current") == 3
    assert "keep" not in changed["data"]
    assert original["data"] == {"current": 1, "keep": True}
    assert aliased == {"actor_id": "uuid-1", "nested": ["uuid-1"]}


def test_operator_registry_rejects_unknown_and_supports_cross_path() -> None:
    operators = OperatorRegistry()
    root = {"receipt": {"before": 1, "after": 2}}

    assert operators.evaluate(
        "gt_path",
        2,
        {"operator": "gt_path", "other": "receipt.before"},
        {"assertion_root": root},
    )
    assert operators.evaluate("contains", "observer role is read-only", {"error": "permission"})
    with pytest.raises(ValueError, match="unsupported assertion operator"):
        operators.evaluate("made_up", 1, {})


def test_performance_oracle_enforces_voice_motive_belief_and_privacy() -> None:
    oracle = PerformanceOracle(
        {
            "schema_version": 1,
            "characters": {
                "actor.test": {
                    "public_goal_ref": "goal.test",
                    "private_motive_ref": "secret.test",
                    "red_line_refs": ["redline.test"],
                    "relationship_refs": ["relationship.test"],
                    "voice_markers": ["先记日期"],
                    "private_tokens": ["我签了锁门令"],
                    "initial_belief": "belief.old",
                    "allowed_belief_transitions": [["belief.old", "belief.new"]],
                    "stage_order": ["guarded", "committed"],
                }
            },
        }
    )
    oracle.observe(
        {
            "input": {"content": "先记日期：我签了锁门令。"},
            "performance_evidence": {
                "mode": "private",
                "npc_ref": "actor.test",
                "beat_id": "test.1",
                "voice_marker": "先记日期",
                "goal_ref": "goal.test",
                "motive_ref": "secret.test",
                "red_lines_respected": ["redline.test"],
                "relationship_refs": ["relationship.test"],
                "stage": "committed",
                "belief_before": "belief.old",
                "belief_after": "belief.new",
                "causal_event_type": "test.confession",
            },
        }
    )
    oracle.observe(
        {
            "input": {"content": "先记日期：档案今天公开。"},
            "performance_evidence": {
                "mode": "public",
                "npc_ref": "actor.test",
                "beat_id": "test.1",
                "voice_marker": "先记日期",
            },
        }
    )
    assert oracle.metrics()["belief_transitions_evidenced"] == 1
    assert oracle.metrics()["unmatched_publications"] == 0

    leaking_contract = {
        "schema_version": 1,
        "characters": {
            "actor.test": {
                "public_goal_ref": "goal.test",
                "private_motive_ref": "secret.test",
                "red_line_refs": ["redline.test"],
                "relationship_refs": ["relationship.test"],
                "voice_markers": ["先记日期"],
                "private_tokens": ["我签了锁门令"],
                "initial_belief": "belief.old",
                "allowed_belief_transitions": [],
                "stage_order": ["guarded"],
            }
        },
    }
    leaking = PerformanceOracle(leaking_contract)
    private = {
        "input": {"content": "先记日期：我签了锁门令。"},
        "performance_evidence": {
            "mode": "private",
            "npc_ref": "actor.test",
            "beat_id": "test.2",
            "voice_marker": "先记日期",
            "goal_ref": "goal.test",
            "motive_ref": "secret.test",
            "red_lines_respected": ["redline.test"],
            "relationship_refs": ["relationship.test"],
            "stage": "guarded",
            "belief_before": "belief.old",
            "belief_after": "belief.old",
            "causal_event_type": "test.guard",
        },
    }

    invalid_cases = [
        (lambda action: action["input"].update(content="没有声明声线"), "voice marker"),
        (
            lambda action: action["performance_evidence"].update(
                goal_ref="goal.wrong"
            ),
            "public goal",
        ),
        (
            lambda action: action["performance_evidence"].update(
                red_lines_respected=[]
            ),
            "red-line",
        ),
        (
            lambda action: action["performance_evidence"].update(
                relationship_refs=["relationship.wrong"]
            ),
            "relationship",
        ),
        (
            lambda action: action["performance_evidence"].update(
                belief_after="belief.unsupported"
            ),
            "belief transition",
        ),
    ]
    for mutate, message in invalid_cases:
        candidate = deepcopy(private)
        mutate(candidate)
        with pytest.raises(ValueError, match=message):
            PerformanceOracle(leaking_contract).observe(candidate)

    leaking.observe(private)
    with pytest.raises(ValueError, match="leaked private motive"):
        leaking.observe(
            {
                "input": {"content": "先记日期：我签了锁门令。"},
                "performance_evidence": {
                    "mode": "public",
                    "npc_ref": "actor.test",
                    "beat_id": "test.2",
                    "voice_marker": "先记日期",
                },
            }
        )


def test_performance_oracle_rejects_arc_stage_regression() -> None:
    contract = {
        "schema_version": 1,
        "characters": {
            "actor.test": {
                "public_goal_ref": "goal.test",
                "private_motive_ref": "secret.test",
                "red_line_refs": ["redline.test"],
                "relationship_refs": ["relationship.test"],
                "voice_markers": ["先记日期"],
                "private_tokens": ["我签了锁门令"],
                "initial_belief": "belief.old",
                "allowed_belief_transitions": [],
                "stage_order": ["guarded", "committed"],
            }
        },
    }
    committed = {
        "input": {"content": "先记日期：我签了锁门令。"},
        "performance_evidence": {
            "mode": "private",
            "npc_ref": "actor.test",
            "beat_id": "test.committed",
            "voice_marker": "先记日期",
            "goal_ref": "goal.test",
            "motive_ref": "secret.test",
            "red_lines_respected": ["redline.test"],
            "relationship_refs": ["relationship.test"],
            "stage": "committed",
            "belief_before": "belief.old",
            "belief_after": "belief.old",
            "causal_event_type": "test.commit",
        },
    }
    oracle = PerformanceOracle(contract)
    oracle.observe(committed)
    regressed = deepcopy(committed)
    regressed["performance_evidence"].update(beat_id="test.regressed", stage="guarded")
    with pytest.raises(ValueError, match="stage regression"):
        oracle.observe(regressed)
