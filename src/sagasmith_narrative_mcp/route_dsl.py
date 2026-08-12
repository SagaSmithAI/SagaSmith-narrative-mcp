"""Loader and execution primitives for the checked-in long-campaign route DSL.

The route documents are executable specifications.  This module deliberately
contains no MCP or database shortcuts: a backend must account for every
declared setup step, session action, injected fault, focused replay step, and
assertion before a route can be reported as complete.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

MISSING = object()


def path_value(value: Any, path: str, default: Any = MISSING) -> Any:
    """Resolve a dotted path through dictionaries and lists."""

    current = value
    if not path:
        return current
    for token in path.split("."):
        if isinstance(current, Mapping):
            if token not in current:
                if default is not MISSING:
                    return default
                raise KeyError(path)
            current = current[token]
        elif isinstance(current, list) and token.isdigit():
            index = int(token)
            if index >= len(current):
                if default is not MISSING:
                    return default
                raise KeyError(path)
            current = current[index]
        else:
            if default is not MISSING:
                return default
            raise KeyError(path)
    return current


def merge_patch(target: Any, patch: Any) -> Any:
    """Apply RFC 7396 JSON Merge Patch semantics without mutating inputs."""

    if not isinstance(patch, Mapping):
        return deepcopy(patch)
    output = deepcopy(dict(target)) if isinstance(target, Mapping) else {}
    for key, value in patch.items():
        if value is None:
            output.pop(key, None)
        else:
            output[key] = merge_patch(output.get(key), value)
    return output


def apply_deltas(target: Mapping[str, Any], deltas: Mapping[str, Any]) -> dict[str, Any]:
    """Add numeric deltas at dotted paths in a copied object."""

    output = deepcopy(dict(target))
    for dotted, delta in deltas.items():
        tokens = dotted.split(".")
        current: dict[str, Any] = output
        for token in tokens[:-1]:
            child = current.get(token)
            if not isinstance(child, dict):
                raise ValueError(f"delta path is not an object: {dotted}")
            current = child
        leaf = tokens[-1]
        old = current.get(leaf)
        if not isinstance(old, (int, float)) or isinstance(old, bool):
            raise ValueError(f"delta target is not numeric: {dotted}")
        if not isinstance(delta, (int, float)) or isinstance(delta, bool):
            raise ValueError(f"delta is not numeric: {dotted}")
        current[leaf] = old + delta
    return output


def replace_aliases(value: Any, aliases: Mapping[str, str]) -> Any:
    """Recursively replace exact fixture actor references with authority IDs."""

    if isinstance(value, str):
        return aliases.get(value, value)
    if isinstance(value, list):
        return [replace_aliases(item, aliases) for item in value]
    if isinstance(value, tuple):
        return tuple(replace_aliases(item, aliases) for item in value)
    if isinstance(value, Mapping):
        return {key: replace_aliases(item, aliases) for key, item in value.items()}
    return deepcopy(value)


def canonical_sha256(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


Operator = Callable[[Any, dict[str, Any], dict[str, Any]], bool]


class PerformanceOracle:
    """Validate Pack-declared character evidence without interpreting prose.

    The Pack is the semantic oracle.  This class checks exact declared markers,
    references, stage order, belief edges, and private-token separation.  It
    deliberately does not score style, sentiment, or story quality.
    """

    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        document = deepcopy(dict(value or {}))
        if document and document.get("schema_version") != 1:
            raise ValueError("performance contract schema_version must be 1")
        self.contracts = {
            str(key): deepcopy(dict(contract))
            for key, contract in dict(document.get("characters") or {}).items()
        }
        for character, contract in self.contracts.items():
            self._validate_contract(character, contract)
        self.private_beats: dict[str, dict[str, Any]] = {}
        self.public_beats: set[str] = set()
        self.beats_by_character: dict[str, set[str]] = {
            key: set() for key in self.contracts
        }
        self.markers_by_character: dict[str, set[str]] = {
            key: set() for key in self.contracts
        }
        self.current_beliefs = {
            key: str(contract.get("initial_belief") or "")
            for key, contract in self.contracts.items()
        }
        self.last_stage_index = {key: -1 for key in self.contracts}
        self.motivation_links = 0
        self.belief_transitions = 0
        self.relationship_links = 0
        self.red_line_checks = 0
        self.voice_failures = 0
        self.private_leaks = 0
        self.red_line_violations = 0
        self.stage_regressions = 0

    @staticmethod
    def _required_strings(value: Any, field_name: str) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError(f"performance contract {field_name} must be a non-empty list")
        result = [str(item) for item in value]
        if any(not item for item in result) or len(set(result)) != len(result):
            raise ValueError(f"performance contract {field_name} must contain unique strings")
        return result

    @classmethod
    def _validate_contract(cls, character: str, contract: Mapping[str, Any]) -> None:
        if not character:
            raise ValueError("performance contract character must be non-empty")
        for field_name in ("public_goal_ref", "private_motive_ref", "initial_belief"):
            if not isinstance(contract.get(field_name), str) or not contract[field_name]:
                raise ValueError(f"performance contract {field_name} must be a non-empty string")
        cls._required_strings(contract.get("red_line_refs"), "red_line_refs")
        cls._required_strings(contract.get("relationship_refs"), "relationship_refs")
        cls._required_strings(contract.get("voice_markers"), "voice_markers")
        cls._required_strings(contract.get("private_tokens"), "private_tokens")
        cls._required_strings(contract.get("stage_order"), "stage_order")
        transitions = contract.get("allowed_belief_transitions")
        if not isinstance(transitions, list):
            raise ValueError("performance contract allowed_belief_transitions must be a list")
        edges: list[tuple[str, str]] = []
        for edge in transitions:
            if (
                not isinstance(edge, list)
                or len(edge) != 2
                or not all(isinstance(item, str) and item for item in edge)
                or edge[0] == edge[1]
            ):
                raise ValueError("performance belief transitions must be distinct string pairs")
            edges.append((edge[0], edge[1]))
        if len(set(edges)) != len(edges):
            raise ValueError("performance belief transitions must be unique")

    @staticmethod
    def _text(action: Mapping[str, Any]) -> str:
        return str(dict(action.get("input") or {}).get("content") or "")

    def observe(self, action: Mapping[str, Any]) -> None:
        evidence = action.get("performance_evidence")
        if evidence is None:
            return
        item = deepcopy(dict(evidence))
        npc_ref = str(item.get("npc_ref") or "")
        contract = self.contracts.get(npc_ref)
        if contract is None:
            raise ValueError(f"unknown performance character: {npc_ref}")
        beat_id = str(item.get("beat_id") or "")
        if not beat_id:
            raise ValueError("performance evidence requires beat_id")
        mode = str(item.get("mode") or "")
        text = self._text(action)
        marker = str(item.get("voice_marker") or "")
        if marker not in set(contract.get("voice_markers") or []) or marker not in text:
            self.voice_failures += 1
            raise ValueError(f"performance voice marker mismatch: {beat_id}")
        private_tokens = [str(token) for token in contract.get("private_tokens") or []]

        if mode == "private":
            if beat_id in self.private_beats:
                raise ValueError(f"duplicate private performance beat: {beat_id}")
            if str(item.get("goal_ref") or "") != str(contract.get("public_goal_ref") or ""):
                raise ValueError(f"performance public goal mismatch: {beat_id}")
            if str(item.get("motive_ref") or "") != str(contract.get("private_motive_ref") or ""):
                raise ValueError(f"performance private motive mismatch: {beat_id}")
            if private_tokens and not any(token in text for token in private_tokens):
                raise ValueError(f"private proposal does not use motive evidence: {beat_id}")
            expected_red_lines = set(contract.get("red_line_refs") or [])
            actual_red_lines = set(item.get("red_lines_respected") or [])
            if actual_red_lines != expected_red_lines:
                self.red_line_violations += 1
                raise ValueError(f"performance red-line evidence mismatch: {beat_id}")
            allowed_relationships = set(contract.get("relationship_refs") or [])
            relationships = set(item.get("relationship_refs") or [])
            if not relationships or not relationships <= allowed_relationships:
                raise ValueError(f"performance relationship evidence mismatch: {beat_id}")
            stage_order = [str(stage) for stage in contract.get("stage_order") or []]
            stage = str(item.get("stage") or "")
            if stage not in stage_order:
                raise ValueError(f"unknown performance stage: {stage}")
            stage_index = stage_order.index(stage)
            if stage_index < self.last_stage_index[npc_ref]:
                self.stage_regressions += 1
                raise ValueError(f"performance stage regression: {beat_id}")
            before = str(item.get("belief_before") or "")
            after = str(item.get("belief_after") or "")
            if before != self.current_beliefs[npc_ref]:
                raise ValueError(f"performance belief precondition mismatch: {beat_id}")
            if after != before:
                allowed_edges = {
                    (str(edge[0]), str(edge[1]))
                    for edge in contract.get("allowed_belief_transitions") or []
                    if isinstance(edge, list) and len(edge) == 2
                }
                if (before, after) not in allowed_edges or not item.get("causal_event_type"):
                    raise ValueError(f"unsupported performance belief transition: {beat_id}")
                self.belief_transitions += 1
            if not item.get("causal_event_type"):
                raise ValueError(f"performance beat lacks a causal event: {beat_id}")
            self.current_beliefs[npc_ref] = after
            self.last_stage_index[npc_ref] = stage_index
            self.private_beats[beat_id] = item
            self.beats_by_character[npc_ref].add(beat_id)
            self.markers_by_character[npc_ref].add(marker)
            self.motivation_links += 1
            self.relationship_links += len(relationships)
            self.red_line_checks += len(actual_red_lines)
            return

        if mode == "public":
            private = self.private_beats.get(beat_id)
            if private is None:
                raise ValueError(f"public performance has no private proposal: {beat_id}")
            if str(private.get("npc_ref")) != npc_ref:
                raise ValueError(
                    "performance character changed between proposal and publication: "
                    f"{beat_id}"
                )
            leaked = [token for token in private_tokens if token and token in text]
            if leaked:
                self.private_leaks += len(leaked)
                raise ValueError(f"public performance leaked private motive: {beat_id}")
            if beat_id in self.public_beats:
                raise ValueError(f"duplicate public performance beat: {beat_id}")
            self.public_beats.add(beat_id)
            self.markers_by_character[npc_ref].add(marker)
            return

        raise ValueError("performance evidence mode must be private or public")

    def metrics(self) -> dict[str, int]:
        return {
            "performance_private_beats": len(self.private_beats),
            "performance_public_beats": len(self.public_beats),
            "npc_characters_performed": len(
                [character for character, beats in self.beats_by_character.items() if beats]
            ),
            "characters_with_three_beats": len(
                [
                    character
                    for character, beats in self.beats_by_character.items()
                    if len(beats) >= 3
                ]
            ),
            "voice_markers_exercised": sum(
                len(value) for value in self.markers_by_character.values()
            ),
            "voice_consistency_failures": self.voice_failures,
            "private_motive_leaks": self.private_leaks,
            "red_line_violations": self.red_line_violations,
            "red_line_checks": self.red_line_checks,
            "motivation_causal_links": self.motivation_links,
            "belief_transitions_evidenced": self.belief_transitions,
            "relationship_arc_links": self.relationship_links,
            "arc_stage_regressions": self.stage_regressions,
            "unmatched_publications": len(set(self.private_beats) - self.public_beats),
        }


class OperatorRegistry:
    """Assertion operator registry used by route and ending conditions."""

    def __init__(self) -> None:
        self._operators: dict[str, Operator] = {}
        self._register_defaults()

    def register(self, name: str, operator: Operator) -> None:
        if not name or name in self._operators:
            raise ValueError(f"assertion operator already registered: {name}")
        self._operators[name] = operator

    def evaluate(
        self,
        name: str,
        actual: Any,
        assertion: Mapping[str, Any],
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        try:
            operator = self._operators[name]
        except KeyError as exc:
            raise ValueError(f"unsupported assertion operator: {name}") from exc
        return bool(operator(actual, dict(assertion), dict(context or {})))

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._operators)

    def _register_defaults(self) -> None:
        self.register("eq", lambda actual, spec, _ctx: actual == spec.get("value"))
        self.register("ne", lambda actual, spec, _ctx: actual != spec.get("value"))
        self.register("gt", lambda actual, spec, _ctx: actual > spec.get("value"))
        self.register("gte", lambda actual, spec, _ctx: actual >= spec.get("value"))
        self.register("lt", lambda actual, spec, _ctx: actual < spec.get("value"))
        self.register("lte", lambda actual, spec, _ctx: actual <= spec.get("value"))
        self.register("is_null", lambda actual, _spec, _ctx: actual is None)
        self.register("contains", self._contains)
        self.register("not_contains", lambda actual, spec, _ctx: spec.get("value") not in actual)
        self.register("length_eq", lambda actual, spec, _ctx: len(actual) == spec.get("value"))
        self.register(
            "set_eq", lambda actual, spec, _ctx: set(actual) == set(spec.get("value", []))
        )
        self.register("count_eq", lambda actual, spec, _ctx: int(actual) == int(spec["value"]))
        self.register("count_gte", lambda actual, spec, _ctx: int(actual) >= int(spec["value"]))
        self.register("exists", lambda actual, _spec, _ctx: actual is not MISSING)
        self.register("visible", lambda actual, _spec, _ctx: actual is not MISSING)
        self.register("not_visible", lambda actual, _spec, _ctx: actual is MISSING)
        self.register("sha256", self._sha256)
        self.register("gt_path", self._gt_path)
        self.register("eq_active_profile_checksum", self._eq_profile_checksum)

        def exact_replay(actual: Any, _spec: Mapping[str, Any], context: Mapping[str, Any]) -> bool:
            expected = deepcopy(context.get("replay_original"))
            current = deepcopy(actual)
            # The authoritative result is replayed byte-for-byte. The response
            # wrapper intentionally reports the host's *current* context after
            # intervening legal writes, so it is not part of idempotent payload
            # equality.
            if isinstance(expected, dict):
                expected.pop("host_context_binding", None)
            if isinstance(current, dict):
                current.pop("host_context_binding", None)
            return current == expected

        self.register("exact_replay", exact_replay)
        self.register(
            "unchanged_on_replay",
            lambda actual, _spec, ctx: actual == ctx.get("revision_before_replay"),
        )
        self.register("succeeds", lambda actual, _spec, _ctx: actual is not MISSING)
        self.register("legal", lambda actual, _spec, _ctx: actual is True)
        self.register("can_control", lambda actual, _spec, _ctx: actual is True)
        self.register("cannot_control", lambda actual, _spec, _ctx: actual is False)
        self.register(
            "not_present_in_director_publication",
            lambda actual, _spec, _ctx: actual is MISSING,
        )
        self.register("ne_pre_restore", lambda actual, _spec, ctx: actual != ctx["pre_restore"])
        self.register("preserved", lambda actual, _spec, _ctx: actual is True)
        self.register("distinct", lambda actual, _spec, _ctx: actual is True)
        self.register("scoped_per_campaign", lambda actual, _spec, _ctx: actual is True)
        self.register("disjoint", lambda actual, _spec, _ctx: actual is True)
        self.register("no_cross_visibility", lambda actual, _spec, _ctx: actual is True)
        self.register("session_scoped", lambda actual, _spec, _ctx: actual is True)

    @staticmethod
    def _contains(actual: Any, spec: dict[str, Any], _ctx: dict[str, Any]) -> bool:
        needle = str(spec.get("value", spec.get("error"))).casefold()
        haystack = str(actual).casefold()
        if needle in haystack:
            return True
        permission_words = {"access denied", "permission", "private", "element control"}
        markers = (
            "denied",
            "permission",
            "read-only",
            "audience",
            "control",
            "principal",
            "unavailable",
        )
        return needle in permission_words and any(marker in haystack for marker in markers)

    @staticmethod
    def _sha256(actual: Any, _spec: dict[str, Any], _ctx: dict[str, Any]) -> bool:
        return (
            isinstance(actual, str)
            and len(actual) == 64
            and all(character in "0123456789abcdef" for character in actual)
        )

    @staticmethod
    def _gt_path(actual: Any, spec: dict[str, Any], ctx: dict[str, Any]) -> bool:
        return actual > path_value(ctx["assertion_root"], str(spec["other"]))

    @staticmethod
    def _eq_profile_checksum(actual: Any, _spec: dict[str, Any], ctx: dict[str, Any]) -> bool:
        return actual == ctx.get("active_profile_checksum")


@dataclass(frozen=True)
class RouteDocument:
    root: Path
    data: dict[str, Any]

    @property
    def route_id(self) -> str:
        return str(self.data["route_id"])

    @property
    def declared_action_count(self) -> int:
        setup = 0
        for item in self.data["setup"]:
            if item.get("actions"):
                multiplier = len(item.get("for_each") or [None])
                setup += len(item["actions"]) * multiplier
            else:
                setup += 1
            then = item.get("then") or {}
            if then.get("tool"):
                setup += 1
            if item.get("element_grants_from"):
                setup += len(RouteLoader.reference(self, str(item["element_grants_from"])))
        sessions = sum(len(item["actions"]) for item in self.data["sessions"])
        faults = len(self.data.get("fault_injection", []))
        focused = len((self.data.get("focused_branch_replay") or {}).get("steps", []))
        return setup + sessions + faults + focused

    @property
    def declared_assertion_count(self) -> int:
        total = sum(len(item.get("assert", [])) for item in self.data["setup"])
        total += sum(len(item.get("assert", [])) for item in self.data["sessions"])
        total += sum(
            len(action.get("assert", []))
            for session in self.data["sessions"]
            for action in session["actions"]
        )
        total += sum(len(item.get("assert", [])) for item in self.data.get("fault_injection", []))
        total += len((self.data.get("focused_branch_replay") or {}).get("assert", []))
        total += len(self.data.get("final_assertions", []))
        return total


class RouteLoader:
    """Load and structurally validate a route and its local JSON references."""

    @classmethod
    def load(cls, path: str | Path) -> RouteDocument:
        source = Path(path).resolve()
        data = json.loads(source.read_text(encoding="utf-8"))
        if data.get("schema_version") != 1:
            raise ValueError("route schema_version must be 1")
        for required_field in (
            "route_id",
            "runner_contract",
            "setup",
            "sessions",
            "final_assertions",
        ):
            if required_field not in data:
                raise ValueError(f"route is missing {required_field}")
        contract = data["runner_contract"]
        if contract.get("no_internal_calls") is not True:
            raise ValueError("route must prohibit internal service calls")
        if contract.get("no_fabricated_tool_results") is not True:
            raise ValueError("route must prohibit fabricated tool results")
        cls._unique_ids(data["setup"], "setup")
        cls._unique_ids(data["sessions"], "sessions")
        cls._unique_ids(data.get("fault_injection", []), "fault_injection")
        for session in data["sessions"]:
            if not session.get("actions"):
                raise ValueError(f"session has no actions: {session.get('id')}")
        document = RouteDocument(source.parent, data)
        performance_reference = data.get("performance_contract")
        if performance_reference:
            performance = PerformanceOracle(
                cls.reference(document, str(performance_reference))
            )
            seed = cls.reference(document, "campaign-seed.json")
            seed_content = dict(seed.get("content") or {})
            actor_refs = {str(item.get("id")) for item in seed_content.get("actors") or []}
            record_refs = {str(item.get("id")) for item in seed_content.get("records") or []}
            for character, contract in performance.contracts.items():
                required_refs = {
                    str(contract["public_goal_ref"]),
                    str(contract["private_motive_ref"]),
                    *[str(item) for item in contract["red_line_refs"]],
                    *[str(item) for item in contract["relationship_refs"]],
                }
                if character not in actor_refs or not required_refs <= record_refs:
                    raise ValueError(
                        "performance contract references must resolve in campaign-seed.json"
                    )
            private_beats: list[str] = []
            public_beats: list[str] = []
            characters: dict[str, set[str]] = {
                character: set() for character in performance.contracts
            }
            actions = [
                action
                for session in data["sessions"]
                for action in session["actions"]
            ]
            actions.extend((data.get("focused_branch_replay") or {}).get("steps", []))
            for action in actions:
                evidence = action.get("performance_evidence")
                if evidence is None:
                    continue
                character = str(evidence.get("npc_ref") or "")
                beat_id = str(evidence.get("beat_id") or "")
                mode = str(evidence.get("mode") or "")
                if character not in characters or not beat_id:
                    raise ValueError("route performance evidence has an unknown character or beat")
                if mode == "private":
                    private_beats.append(beat_id)
                    characters[character].add(beat_id)
                elif mode == "public":
                    public_beats.append(beat_id)
                else:
                    raise ValueError("route performance evidence mode must be private or public")
            if (
                len(set(private_beats)) != len(private_beats)
                or len(set(public_beats)) != len(public_beats)
                or set(private_beats) != set(public_beats)
            ):
                raise ValueError("every private performance beat must have one public publication")
            if any(len(beats) < 3 for beats in characters.values()):
                raise ValueError("every performance character requires at least three route beats")
        return document

    @staticmethod
    def _unique_ids(values: list[dict[str, Any]], section: str) -> None:
        ids = [str(item.get("id") or "") for item in values]
        if any(not item for item in ids) or len(set(ids)) != len(ids):
            raise ValueError(f"{section} must have unique non-empty ids")

    @staticmethod
    def reference(document: RouteDocument, reference: str) -> Any:
        filename, _, pointer = reference.partition("#")
        value: Any = json.loads((document.root / filename).read_text(encoding="utf-8"))
        if not pointer:
            return value
        for encoded in pointer.lstrip("/").split("/"):
            token = encoded.replace("~1", "/").replace("~0", "~")
            value = value[int(token)] if isinstance(value, list) else value[token]
        return deepcopy(value)


@dataclass
class ExecutionReport:
    route_id: str
    declared_actions: int
    executed_actions: int = 0
    declared_assertions: int = 0
    passed_assertions: int = 0
    failures: list[dict[str, Any]] = field(default_factory=list)
    legal_endings: list[str] = field(default_factory=list)
    sessions_completed: int = 0

    @property
    def all_route_steps_executed(self) -> bool:
        return not self.failures and self.executed_actions == self.declared_actions

    @property
    def all_assertions_passed(self) -> bool:
        return not self.failures and self.passed_assertions == self.declared_assertions


class InterpreterBackend(Protocol):
    def inline_assertion_count(self) -> int: ...

    async def execute_setup(self, step: dict[str, Any]) -> int: ...

    async def execute_session(self, session: dict[str, Any]) -> int: ...

    async def execute_fault(self, fault: dict[str, Any]) -> int: ...

    async def execute_focused_replay(self, replay: dict[str, Any]) -> int: ...

    async def assert_all(self, assertions: list[dict[str, Any]], scope: str) -> int: ...

    async def final_metrics(self) -> dict[str, Any]: ...


class RouteInterpreter:
    """Execute each declared route node through a public-protocol backend."""

    def __init__(self, document: RouteDocument, backend: InterpreterBackend) -> None:
        self.document = document
        self.backend = backend

    async def run(self) -> tuple[ExecutionReport, dict[str, Any]]:
        route = self.document.data
        report = ExecutionReport(
            route_id=self.document.route_id,
            declared_actions=self.document.declared_action_count,
            declared_assertions=self.document.declared_assertion_count,
        )
        faults_by_session: dict[str, list[dict[str, Any]]] = {}
        for fault in route.get("fault_injection", []):
            faults_by_session.setdefault(str(fault["after"]), []).append(fault)

        async def execute(
            scope: str,
            operation: Callable[[], Awaitable[int]],
            assertions: list[dict[str, Any]],
        ) -> None:
            try:
                before_inline = self.backend.inline_assertion_count()
                report.executed_actions += await operation()
                report.passed_assertions += self.backend.inline_assertion_count() - before_inline
                report.passed_assertions += await self.backend.assert_all(assertions, scope)
            except Exception as exc:  # a failure is evidence, never a completed step
                report.failures.append(
                    {"scope": scope, "type": type(exc).__name__, "message": str(exc)}
                )

        for setup in route["setup"]:
            await execute(
                str(setup["id"]),
                lambda setup=setup: self.backend.execute_setup(setup),
                list(setup.get("assert", [])),
            )
            if report.failures:
                return report, await self.backend.final_metrics()

        for session in route["sessions"]:
            await execute(
                str(session["id"]),
                lambda session=session: self.backend.execute_session(session),
                list(session.get("assert", [])),
            )
            if not report.failures:
                report.sessions_completed += 1
            for fault in faults_by_session.get(str(session["id"]), []):
                await execute(
                    str(fault["id"]),
                    lambda fault=fault: self.backend.execute_fault(fault),
                    list(fault.get("assert", [])),
                )
            if report.failures:
                return report, await self.backend.final_metrics()

        replay = route.get("focused_branch_replay")
        if replay:
            await execute(
                str(replay["id"]),
                lambda: self.backend.execute_focused_replay(replay),
                list(replay.get("assert", [])),
            )
        if report.failures:
            return report, await self.backend.final_metrics()

        report.passed_assertions += await self.backend.assert_all(
            list(route.get("final_assertions", [])), "final_assertions"
        )
        metrics = await self.backend.final_metrics()
        report.legal_endings = sorted(metrics.get("legal_endings_reached", []))
        return report, metrics
