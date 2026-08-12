"""Stable narrative document and public argument validation."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping

PHASE_LOBBY = "lobby"
PHASE_PLAY = "play"
PHASE_CONFLICT = "conflict"
PHASES = frozenset({PHASE_LOBBY, PHASE_PLAY, PHASE_CONFLICT})
RECORD_KINDS = frozenset(
    {
        "relationship",
        "faction",
        "clock",
        "resource",
        "tag",
        "status",
        "goal",
        "thread",
        "clue",
        "secret",
        "rumor",
        "location",
        "route",
        "travel_leg",
        "commitment",
        "consequence",
    }
)
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.:-]{0,127}$")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def required_id(value: Any, field: str) -> str:
    normalized = str(value or "").strip()
    if not ID_PATTERN.fullmatch(normalized):
        raise ValueError(f"{field} must be a namespaced lowercase identifier")
    return normalized


def required_text(value: Any, field: str, *, limit: int = 4000) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if not normalized or len(normalized) > limit:
        raise ValueError(f"{field} must contain 1 to {limit} characters")
    return normalized


def initial_document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "phase": PHASE_LOBBY,
        "profiles": {"drafts": {}, "finalized": {}, "active": None},
        "packs": {"drafts": {}, "finalized": {}, "imports": {}, "active": []},
        "records": {},
        "scenes": {},
        "active_scene_id": None,
        "element_grants": [],
        "actor_bindings": {},
        "npc_conversations": {},
        "conflict": None,
        "random_stream": {"seed": None, "cursor": 0},
        "settlements": [],
    }


def narrative_document(state: Mapping[str, Any] | None) -> dict[str, Any]:
    state_value = dict(state or {})
    current = state_value.get("narrative")
    if current is None:
        return initial_document()
    if not isinstance(current, Mapping) or current.get("schema_version") != 1:
        raise ValueError("unsupported narrative campaign document")
    result = deepcopy(dict(current))
    if result.get("phase") not in PHASES:
        raise ValueError("invalid narrative phase")
    return result


def state_with_narrative(state: Mapping[str, Any], document: Mapping[str, Any]) -> dict[str, Any]:
    result = deepcopy(dict(state))
    result["narrative"] = deepcopy(dict(document))
    return result


def active_profile(document: Mapping[str, Any]) -> dict[str, Any] | None:
    profiles = dict(document.get("profiles") or {})
    active_key = profiles.get("active")
    if not active_key:
        return None
    profile = dict(profiles.get("finalized") or {}).get(str(active_key))
    return deepcopy(dict(profile)) if isinstance(profile, Mapping) else None


def validate_profile(value: Mapping[str, Any], *, finalized: bool = False) -> dict[str, Any]:
    profile = deepcopy(dict(value))
    profile_id = required_id(profile.get("id"), "profile.id")
    version = required_text(profile.get("version"), "profile.version", limit=64)
    level = int(profile.get("mechanics_level", 0))
    if level not in {0, 1}:
        raise ValueError("mechanics_level must be 0 or 1")
    capabilities = sorted(
        {required_id(item, "profile capability") for item in profile.get("capabilities", [])}
    )
    mechanics = list(profile.get("mechanics") or [])
    if level == 0 and mechanics:
        raise ValueError("Level 0 profiles cannot declare mechanics")
    seen: set[str] = set()
    normalized_mechanics = []
    for raw in mechanics:
        item = deepcopy(dict(raw))
        mechanic_id = required_id(item.get("id"), "mechanic.id")
        if mechanic_id in seen:
            raise ValueError(f"duplicate mechanic: {mechanic_id}")
        seen.add(mechanic_id)
        kind = str(item.get("kind") or "")
        if kind not in {"dice_pool", "table", "track_delta", "resource_delta"}:
            raise ValueError(f"unsupported Level 1 mechanic kind: {kind}")
        if kind == "dice_pool":
            sides = int(item.get("sides", 6))
            if sides < 2 or sides > 1000:
                raise ValueError("dice_pool sides must be between 2 and 1000")
            item["sides"] = sides
            item["max_dice"] = min(100, max(1, int(item.get("max_dice", 20))))
            bands = list(item.get("bands") or [])
            if not bands:
                raise ValueError("dice_pool requires result bands")
            covered: set[int] = set()
            normalized_bands = []
            for raw_band in bands:
                band = deepcopy(dict(raw_band))
                minimum = int(band.get("minimum", 1))
                maximum = int(band.get("maximum", sides))
                if minimum < 1 or maximum > sides or minimum > maximum:
                    raise ValueError("dice result band is outside die bounds")
                values = set(range(minimum, maximum + 1))
                if covered & values:
                    raise ValueError("dice result bands must not overlap")
                covered |= values
                band["minimum"] = minimum
                band["maximum"] = maximum
                normalized_bands.append(band)
            if covered != set(range(1, sides + 1)):
                raise ValueError("dice result bands must cover every possible result")
            item["bands"] = normalized_bands
        elif kind == "table":
            entries = list(item.get("entries") or [])
            if not entries or len(entries) > 1000:
                raise ValueError("table requires 1 to 1000 entries")
            if any(not isinstance(entry, Mapping) for entry in entries):
                raise ValueError("table entries must be objects")
        else:
            item["minimum"] = int(item.get("minimum", 0))
            item["maximum"] = int(item.get("maximum", 100))
            if item["minimum"] > item["maximum"]:
                raise ValueError("mechanic minimum cannot exceed maximum")
        normalized_mechanics.append(item)
    result = {
        "id": profile_id,
        "version": version,
        "title": required_text(profile.get("title") or profile_id, "profile.title", limit=200),
        "mechanics_level": level,
        "capabilities": capabilities,
        "authority": deepcopy(dict(profile.get("authority") or {})),
        "actor_schema": deepcopy(dict(profile.get("actor_schema") or {})),
        "record_extensions": deepcopy(dict(profile.get("record_extensions") or {})),
        "mechanics": normalized_mechanics,
        "sources": deepcopy(list(profile.get("sources") or [])),
    }
    result["checksum"] = checksum(result)
    if finalized and not result["sources"]:
        raise ValueError("profile finalization requires at least one source/evidence record")
    return result


def validate_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = deepcopy(dict(value))
    record_id = required_id(record.get("id"), "record.id")
    kind = str(record.get("kind") or "")
    if kind not in RECORD_KINDS and not kind.startswith("profile:"):
        raise ValueError(f"unsupported narrative record kind: {kind}")
    revision = int(record.get("revision", 0))
    if revision < 0:
        raise ValueError("record revision cannot be negative")
    audience = deepcopy(dict(record.get("audience") or {"scope": "table"}))
    scope = str(audience.get("scope") or "")
    if scope not in {"table", "public", "group", "actor", "facilitator", "private_worker"}:
        raise ValueError("unsupported audience scope")
    if scope == "group" and not (audience.get("principal_ids") or audience.get("actor_ids")):
        raise ValueError("group audience requires principal_ids or actor_ids")
    if scope == "actor" and not (audience.get("actor_id") or audience.get("actor_ids")):
        raise ValueError("actor audience requires actor_id or actor_ids")
    if scope == "private_worker" and not (
        audience.get("principal_id") or audience.get("worker_id")
    ):
        raise ValueError("private_worker audience requires principal_id or worker_id")
    return {
        "id": record_id,
        "kind": kind,
        "title": required_text(record.get("title") or record_id, "record.title", limit=200),
        "status": required_id(record.get("status") or "active", "record.status"),
        "revision": revision,
        "audience": audience,
        "controller": deepcopy(dict(record.get("controller") or {})),
        "source": deepcopy(dict(record.get("source") or {})),
        "data": deepcopy(dict(record.get("data") or {})),
    }
