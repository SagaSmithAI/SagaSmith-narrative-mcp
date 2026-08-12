# Profile and Pack lifecycle

## Game profiles

A profile is an immutable, checksummed declaration activated per campaign.
Drafts may be revised; finalization is the trust boundary, and changes after it
require a new version.

Level 0 declares no MCP-resolved mechanics. Human and Agent rulings remain
explicit and are committed as authorized consequences. Level 1 may declare only
bounded `dice_pool`, `table`, `track_delta`, and `resource_delta` mechanics.
Dice bands must cover the complete result space without gaps or overlaps.
Random mechanics advance a campaign-local stream and return replayable receipts.

Capabilities control native exposure, including mechanics, optional conflict,
NPC conversation, downtime, and world turns. `authority` declares facilitator
roles, owned-actor conflict control, and distributed element stewardship.
`actor_schema` validates actor sheets. `record_extensions` may declare required
data fields for a record kind. Human-language invariant notes remain review
evidence; the MCP does not pretend to execute prose.

Promote a game to an independent system package when its reusable mechanics
require more than these bounded declarations. Do not encode a rulebook in a
Skill, infer it from Pack prose, or add a one-game branch to this MCP.

## Packs

The current Pack kinds are:

- `content`: reusable setting, people, locations, and narrative material;
- `module`: scenes, routes, endings, and playable scenario material;
- `campaign_seed`: initial principals, membership, actors, grants, records, and
  stable logical references for one campaign.

All follow `create_draft -> update_draft -> finalize -> import -> activate`.
Finalization requires source evidence, an explicit Agent finalization decision,
and a distribution-rights decision. Finalized `id@version` values cannot be
silently reopened or replaced. Activation validates the active profile id,
version, optional checksum, required and forbidden capabilities, and active Pack
dependencies. Seed application is idempotent and atomic; reference collisions
are rejected rather than guessed.

Private copyrighted material may be used only according to its recorded rights
and distribution decision. The MCP stores evidence and enforces the declared
boundary; the Agent is responsible for evidence review. Module generation can
help draft structures only when it produces this schema and stays before the
same review/finalization boundary. D&D-specific generation templates are not a
general narrative importer.

## Included regression Packs

`ash-harbor` is a facilitator-led Level 1 campaign with factions, relationships,
clocks, resources, private knowledge, deterministic random receipts, downtime,
world turns, isolated NPC dialogue, and optional conflict.

`moss-road-seasons` is a Level 0, diceless campaign with no fixed facilitator.
It uses rotating scene-scoped element stewardship, travel, promises,
relationships, private knowledge, downtime, and world evolution. Conflict and
mechanic tools must remain absent.

Both are self-authored under Apache-2.0. Their route documents contain ten
sessions, negative authorization and concurrency assertions, a snapshot restore,
and a focused alternate legal ending. The parallel regression runner must prove
separate campaign, branch, idempotency, audience, exposure, knowledge, and
random-stream state.
