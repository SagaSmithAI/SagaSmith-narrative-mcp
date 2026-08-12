from __future__ import annotations

from pathlib import Path

import pytest

from sagasmith_narrative_mcp.route_dsl import (
    OperatorRegistry,
    RouteLoader,
    apply_deltas,
    merge_patch,
    path_value,
    replace_aliases,
)

ROOT = Path(__file__).parents[1]


@pytest.mark.parametrize("fixture", ["ash-harbor", "moss-road-seasons"])
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
