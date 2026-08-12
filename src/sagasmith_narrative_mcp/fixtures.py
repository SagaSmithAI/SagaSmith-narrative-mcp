"""Original regression fixture loader and structural validator."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import validate_profile

REQUIRED_FILES = (
    "manifest.json",
    "profile.json",
    "module.json",
    "campaign-seed.json",
    "provenance.json",
    "route.json",
)


def load_fixture(path: str | Path) -> dict[str, Any]:
    root = Path(path).resolve()
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(f"fixture is missing files: {', '.join(missing)}")
    values = {
        name.removesuffix(".json").replace("-", "_"): json.loads(
            (root / name).read_text(encoding="utf-8")
        )
        for name in REQUIRED_FILES
    }
    profile = validate_profile(values["profile"])
    route = values["route"]
    if route.get("schema_version") != 1 or not route.get("sessions"):
        raise ValueError("route must contain schema_version 1 and sessions")
    if route.get("runner_contract", {}).get("no_internal_calls") is not True:
        raise ValueError("fixture route must prohibit internal service calls")
    packs = [values["campaign_seed"], values["module"]]
    for pack in packs:
        if pack.get("review", {}).get("agent_finalization") is not True:
            raise ValueError("fixture Pack requires Agent finalization evidence")
        if pack.get("rights", {}).get("distribution") != "public":
            raise ValueError("public original fixture Pack must be distributable")
    return {
        "root": str(root),
        "id": str(values["manifest"].get("id") or root.name),
        "manifest": values["manifest"],
        "profile": profile,
        "packs": packs,
        "route": route,
        "session_count": len(route["sessions"]),
    }
