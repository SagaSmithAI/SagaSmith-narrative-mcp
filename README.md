# SagaSmith Narrative MCP

An authoritative, system-neutral MCP for long-form tabletop narrative play. It
depends on `sagasmith-core` for durable campaigns, transactions, revisions,
documents, continuity ledgers, snapshots, and branches.

The base runtime has two phases: `lobby` and `play`. A profile may opt into an
authoritative `conflict` phase. Native MCP tools are session-scoped and dynamic;
hosts must process `tools/list_changed` notifications.

The server never guesses rules from prose. A campaign binds an immutable profile
version and checksum. Profiles may use Level 0 (explicit Agent/human rulings) or
the limited pure Level 1 mechanics in this package. More complex rules require a
separate system provider.

Lobby administrators can inspect campaign-scoped profile/Pack drafts through
`narrative_query`. Access changes support grant and revoke operations with
last-owner protection; controlled actors can be updated through `actor_change`.
Recovery queries and snapshot/branch mutations are administrator-only.
Snapshots remain independently restorable full state documents at the public
boundary. Core schema v8 stores each document as one bounded, checksummed
`zlib-1` record; restore and branch checkout do not replay an ancestor chain.

See [Architecture and authority](docs/architecture.md) and
[Profile and Pack lifecycle](docs/profile-and-pack.md) for the durable product
boundary. Three self-authored regression campaigns live in `fixtures/ash-harbor`,
`fixtures/moss-road-seasons`, and `fixtures/echo-manor-voices`. The third is a
15-session, five-NPC character-performance campaign with declared goals,
private motives, red lines, false beliefs, relationship arcs, voice markers,
isolated dialogue, and a recovered alternate ending.

## Development

```powershell
python -m pip install -e ".[dev]"
pytest
ruff check .
```

Run the original campaign fixtures concurrently through real stdio MCP sessions:

```powershell
python scripts/regression_parallel_campaigns.py --output .runs/parallel
```

The runner opens real stdio MCP sessions, uses a separate session identity for
each principal, executes every declared route step, follows a focused alternate
branch, and emits machine-readable per-campaign timelines and a combined
summary. A non-zero exit means the run is not accepted.

The Agent Host integration uses the MCP and Agent repositories' own environments: the
test process runs with `../SagaSmith-agent/.venv/Scripts/python.exe`, while the
spawned MCP server runs with this repository's `.venv/Scripts/python.exe`:

```powershell
../SagaSmith-agent/.venv/Scripts/python.exe -m pytest -q tests/test_agent_host_integration.py
```

Run locally with `sagasmith-narrative-mcp`. Its independent default home is
`~/.sagasmith/narrative-mcp`. Set `SAGASMITH_NARRATIVE_MCP_HOME` to relocate it,
`SAGASMITH_NARRATIVE_MCP_DATABASE_URL` to use an explicit database, and
`SAGASMITH_NARRATIVE_MCP_BOUND_PRINCIPAL_ID` when the transport authenticates
one principal.

The server applies Core Alembic migrations at startup. Before the first launch
with Snapshot schema v8, stop the server and take a consistent backup of the
SQLite database (including a settled WAL), or use the external database's
native backup mechanism. The one-time cutover accepts complete, checksum-valid
schema-v7 snapshots and removes the old JSON `payload` column. Schema v3-v6
snapshots must first be materialized by their matching historical runtime.
There is no database downgrade or dual-protocol mode: rollback means restoring
the pre-upgrade database backup together with the matching Core and MCP
versions.

Without `SAGASMITH_NARRATIVE_MCP_BOUND_PRINCIPAL_ID`, stdio is a trusted
single-user local mode; model-supplied principal fields are not multiplayer
authentication. A multiplayer deployment must bind one authenticated principal
per MCP process through a trusted transport. Shared-principal HTTP exposure is
not currently supported and the server must not be published directly to a
network.
