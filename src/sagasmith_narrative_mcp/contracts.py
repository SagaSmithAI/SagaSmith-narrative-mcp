"""Stable narrative document and public argument validation."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from typing import Any, Mapping

import jsonschema

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
AUDIENCE_SCOPES = frozenset(
    {"table", "public", "group", "actor", "facilitator", "private_worker"}
)
CONTROLLER_SCOPES = frozenset(
    {"actor", "element", "facilitator", "group", "principal", "rotating", "steward"}
)
CAMPAIGN_ROLES = frozenset({"owner", "dm", "player", "observer"})


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


def _text_list(value: Any, field: str, *, limit: int = 128) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError(f"{field} must be an array with at most {limit} entries")
    result = [required_text(item, field, limit=200) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} entries must be unique")
    return result


def validate_audience(
    value: Mapping[str, Any] | None, *, field: str = "audience"
) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    audience = deepcopy(dict(value or {"scope": "table"}))
    unknown = set(audience) - {
        "scope",
        "principal_id",
        "principal_ids",
        "actor_id",
        "actor_ids",
        "worker_id",
    }
    if unknown:
        raise ValueError(f"{field} has unsupported fields: {', '.join(sorted(unknown))}")
    scope = str(audience.get("scope") or "")
    if scope not in AUDIENCE_SCOPES:
        raise ValueError(f"unsupported {field} scope")
    result: dict[str, Any] = {"scope": scope}
    for key in ("principal_id", "actor_id", "worker_id"):
        if audience.get(key) is not None:
            result[key] = required_text(audience[key], f"{field}.{key}", limit=200)
    for key in ("principal_ids", "actor_ids"):
        values = _text_list(audience.get(key), f"{field}.{key}")
        if values:
            result[key] = values
    if scope in {"table", "public", "facilitator"} and set(result) != {"scope"}:
        raise ValueError(f"{field} scope {scope} cannot declare audience targets")
    if scope == "group" and not (result.get("principal_ids") or result.get("actor_ids")):
        raise ValueError(f"{field} group scope requires principal_ids or actor_ids")
    if scope == "actor" and not (result.get("actor_id") or result.get("actor_ids")):
        raise ValueError(f"{field} actor scope requires actor_id or actor_ids")
    if scope == "private_worker" and not result.get("principal_id"):
        raise ValueError(f"{field} private_worker scope requires principal_id")
    return result


def validate_controller(
    value: Mapping[str, Any] | None, *, field: str = "controller"
) -> dict[str, Any]:
    if value is not None and not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    controller = deepcopy(dict(value or {}))
    unknown = set(controller) - {
        "scope",
        "principal_id",
        "principal_ids",
        "actor_id",
        "actor_ids",
        "element_ref",
    }
    if unknown:
        raise ValueError(f"{field} has unsupported fields: {', '.join(sorted(unknown))}")
    result: dict[str, Any] = {}
    if controller.get("scope") is not None:
        scope = str(controller["scope"])
        if scope not in CONTROLLER_SCOPES:
            raise ValueError(f"unsupported {field} scope")
        result["scope"] = scope
    for key in ("principal_id", "actor_id", "element_ref"):
        if controller.get(key) is not None:
            result[key] = required_text(controller[key], f"{field}.{key}", limit=200)
    for key in ("principal_ids", "actor_ids"):
        values = _text_list(controller.get(key), f"{field}.{key}")
        if values:
            result[key] = values
    scope = result.get("scope")
    if scope == "facilitator" and set(result) != {"scope"}:
        raise ValueError(f"{field} facilitator scope cannot declare other controllers")
    if scope == "rotating" and not result.get("principal_ids"):
        raise ValueError(f"{field} rotating scope requires principal_ids")
    if scope == "steward" and not result.get("principal_id"):
        raise ValueError(f"{field} steward scope requires principal_id")
    if scope == "group" and not (result.get("principal_ids") or result.get("actor_ids")):
        raise ValueError(f"{field} group scope requires principal_ids or actor_ids")
    return result


def validate_sources(
    value: Any, *, field: str = "sources", finalized: bool = False
) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError(f"{field} must be an array with at most 256 entries")
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError(f"{field}[{index}] must be an object")
        item = deepcopy(dict(raw))
        source_kind = item.get("type", item.get("kind"))
        if source_kind is None:
            raise ValueError(f"{field}[{index}] requires type or kind")
        required_text(source_kind, f"{field}[{index}].type", limit=100)
        locators = [item.get(name) for name in ("citation", "ref", "url", "path")]
        if finalized and not any(str(locator or "").strip() for locator in locators):
            raise ValueError(f"{field}[{index}] requires a reproducible evidence locator")
        result.append(item)
    if finalized and not result:
        raise ValueError(f"{field} requires at least one evidence record")
    return result


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
    if level == 1 and (not mechanics or "mechanics" not in capabilities):
        raise ValueError("Level 1 profiles require mechanics and the mechanics capability")
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
    raw_authority = profile.get("authority") or {}
    if not isinstance(raw_authority, Mapping):
        raise ValueError("profile.authority must be an object")
    authority = deepcopy(dict(raw_authority))
    facilitator_roles = authority.get("facilitator_roles")
    if facilitator_roles is not None:
        roles = _text_list(facilitator_roles, "profile.authority.facilitator_roles", limit=4)
        if set(roles) - (CAMPAIGN_ROLES - {"observer"}):
            raise ValueError("facilitator_roles contains an unsupported campaign role")
        authority["facilitator_roles"] = roles
    audience_scopes = authority.get("audience_scopes")
    if audience_scopes is not None:
        scopes = _text_list(audience_scopes, "profile.authority.audience_scopes", limit=16)
        if set(scopes) - AUDIENCE_SCOPES:
            raise ValueError("profile authority contains an unsupported audience scope")
        authority["audience_scopes"] = scopes
    for name in (
        "conflict_is_available",
        "conflict_is_optional",
        "element_stewardship",
        "fixed_facilitator_required",
        "player_controls_owned_actor",
        "stewardship_rotates_by_scene",
        "world_turn_may_be_proposed_by_active_steward",
        "world_turn_requires_facilitator",
    ):
        if name in authority and not isinstance(authority[name], bool):
            raise ValueError(f"profile.authority.{name} must be boolean")
    raw_actor_schema = profile.get("actor_schema") or {}
    if not isinstance(raw_actor_schema, Mapping):
        raise ValueError("profile.actor_schema must be an object")
    actor_schema = deepcopy(dict(raw_actor_schema))
    try:
        jsonschema.Draft202012Validator.check_schema(actor_schema)
    except jsonschema.SchemaError as error:
        raise ValueError(f"profile.actor_schema is invalid: {error.message}") from error
    raw_extensions = profile.get("record_extensions") or {}
    if not isinstance(raw_extensions, Mapping):
        raise ValueError("profile.record_extensions must be an object")
    record_extensions: dict[str, Any] = {}
    for raw_kind, raw_extension in raw_extensions.items():
        kind = str(raw_kind)
        if kind not in RECORD_KINDS and not kind.startswith("profile:"):
            raise ValueError(f"profile record extension has unsupported kind: {kind}")
        if not isinstance(raw_extension, Mapping):
            raise ValueError(f"profile.record_extensions.{kind} must be an object")
        extension = deepcopy(dict(raw_extension))
        extension["required_data"] = _text_list(
            extension.get("required_data"),
            f"profile.record_extensions.{kind}.required_data",
        )
        record_extensions[kind] = extension
    sources = validate_sources(profile.get("sources") or [], finalized=finalized)
    result = {
        "id": profile_id,
        "version": version,
        "title": required_text(profile.get("title") or profile_id, "profile.title", limit=200),
        "mechanics_level": level,
        "capabilities": capabilities,
        "authority": authority,
        "actor_schema": actor_schema,
        "record_extensions": record_extensions,
        "mechanics": normalized_mechanics,
        "sources": sources,
    }
    result["checksum"] = checksum(result)
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
    audience = validate_audience(record.get("audience"), field="record.audience")
    return {
        "id": record_id,
        "kind": kind,
        "title": required_text(record.get("title") or record_id, "record.title", limit=200),
        "status": required_id(record.get("status") or "active", "record.status"),
        "revision": revision,
        "audience": audience,
        "controller": validate_controller(record.get("controller"), field="record.controller"),
        "source": deepcopy(dict(record.get("source") or {})),
        "data": deepcopy(dict(record.get("data") or {})),
    }
