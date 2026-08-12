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
        return RouteDocument(source.parent, data)

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
