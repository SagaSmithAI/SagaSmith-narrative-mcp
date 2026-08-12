"""Server configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from sagasmith_core.database import sqlite_database_url


@dataclass(frozen=True)
class McpConfig:
    database_url: str | None = None
    bound_principal_id: str | None = None

    @classmethod
    def from_environment(cls) -> "McpConfig":
        configured_url = os.environ.get("SAGASMITH_NARRATIVE_MCP_DATABASE_URL")
        if configured_url is None:
            configured_home = os.environ.get("SAGASMITH_NARRATIVE_MCP_HOME")
            home = (
                Path(configured_home).expanduser()
                if configured_home
                else Path.home() / ".sagasmith" / "narrative-mcp"
            )
            home.mkdir(parents=True, exist_ok=True)
            configured_url = sqlite_database_url(home / "narrative.db")
        return cls(
            database_url=configured_url,
            bound_principal_id=os.environ.get("SAGASMITH_NARRATIVE_MCP_BOUND_PRINCIPAL_ID"),
        )
