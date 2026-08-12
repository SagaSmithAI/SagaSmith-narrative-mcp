"""Ephemeral session-native tool exposure."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable
from uuid import uuid4

from .policies import CORE_TOOLS, policy_for_tool


class ExposureError(ValueError):
    pass


@dataclass
class Exposure:
    id: str
    session_key: str
    principal_id: str
    campaign_id: str | None
    phase: str
    revision: int = 0
    loaded_tools: set[str] = field(default_factory=set)


class ExposureRegistry:
    def __init__(self) -> None:
        self._items: dict[str, Exposure] = {}

    def open(
        self, session_key: str, principal_id: str, campaign_id: str | None, phase: str
    ) -> Exposure:
        value = Exposure(f"exp_{uuid4().hex}", session_key, principal_id, campaign_id, phase)
        self._items[session_key] = value
        return value

    def active(self, session_key: str) -> Exposure | None:
        return self._items.get(session_key)

    def for_campaign(self, campaign_id: str) -> list[Exposure]:
        return [item for item in self._items.values() if item.campaign_id == campaign_id]

    def refresh(self, exposure: Exposure, phase: str, allowed: set[str]) -> bool:
        retained = exposure.loaded_tools & allowed
        changed = exposure.phase != phase or retained != exposure.loaded_tools
        if changed:
            exposure.phase = phase
            exposure.loaded_tools = retained
            exposure.revision += 1
        return changed

    def set_tools(
        self, exposure: Exposure, add: Iterable[str], remove: Iterable[str], allowed: set[str]
    ) -> bool:
        additions = {str(item).strip() for item in add if str(item).strip()}
        removals = {str(item).strip() for item in remove if str(item).strip()}
        if additions & removals:
            raise ExposureError("the same tool cannot be added and removed")
        for name in additions:
            if policy_for_tool(name) is None or name not in allowed:
                raise ExposureError(f"tool is unavailable in this context: {name}")
        updated = (exposure.loaded_tools | additions) - removals
        changed = updated != exposure.loaded_tools
        if changed:
            exposure.loaded_tools = updated
            exposure.revision += 1
        return changed

    @staticmethod
    def visible(exposure: Exposure | None) -> set[str]:
        return set(CORE_TOOLS) | (set(exposure.loaded_tools) if exposure else set())

    @staticmethod
    def require(exposure: Exposure, name: str) -> None:
        if name not in CORE_TOOLS and name not in exposure.loaded_tools:
            raise ExposureError(f"tool is not loaded for this session: {name}")

    @staticmethod
    def status(exposure: Exposure) -> dict[str, object]:
        return {
            "exposure_id": exposure.id,
            "revision": exposure.revision,
            "principal_id": exposure.principal_id,
            "campaign_id": exposure.campaign_id,
            "phase": exposure.phase,
            "loaded_tools": sorted(exposure.loaded_tools),
            "visible_tools": sorted(ExposureRegistry.visible(exposure)),
        }
