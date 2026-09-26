# Command Grid redesign

## Decision

Agentlog owns session identity, presence, lineage, transcripts, provenance, and
evidence. The Command Grid and Command Floor are read-only projections of those
contracts. They do not install hooks, scan transcripts, or persist a second
agent lifecycle.

The approved visual direction combines a galactic command deck with a
cyber-industrial execution grid. Human-facing sessions appear as stable crew
operators. Background workers appear as mechanical familiars linked to their
parent. Unknown relationships and activity remain visibly unknown.

## Usage

```ts
const projection = projectCommandFloor(presence.sessions);

<CommandFloor
  projection={projection}
  selectedActorId={selectedActorId}
  onSelectActor={setSelectedActorId}
/>
```

Selecting an actor sets one session identity. The Command Floor, runtime lanes,
topology, trace stream, and evidence inspector all resolve from that identity.

## Ownership

- `api.ts` owns transport payload types.
- `useLivePresence.ts` owns snapshot ordering, SSE reconciliation, and fallback
  polling.
- `commandFloor.ts` owns the pure projection from `LiveSession[]` to rooms,
  operators, familiars, specialties, stations, and explicit lineage states.
- Command Grid components own rendering and local selection only.
- Session detail remains the source for transcript, tools, artifacts, and
  provenance.

## Domain model

Actor state is a discriminated union. A worker cannot silently masquerade as a
root operator, and a missing parent is represented as `unattached`, not `null`
plus a comment.

Specialty is presentation metadata, not a new fact in the ledger. It is marked
`observed` only when a current tool supports it, `inferred` when derived from
task language, and `unknown` otherwise.

## Flow

```text
harness artifacts
  -> Agentlog watcher and ingest
  -> /api/live snapshot + SSE presence frames
  -> useLivePresence ordering and reconciliation
  -> projectCommandFloor
  -> Command Floor, runtime lanes, queue, and inspector
  -> existing session detail and evidence routes
```

## Alternatives considered

### Run Pixel Agents beside Agentlog

Rejected. It creates duplicate transcript scanners, hook ownership, lifecycle
state, persistence, authentication, and answers to "what is this agent doing?"

### Embed or fork Pixel Agents' complete renderer

Deferred. Its Canvas engine is coupled to its office state, asset system, and
transport. The bundled non-character asset licensing is also not explicit
enough for reuse without clarification.

### Agentlog-native projection and renderer

Chosen. It preserves one source of truth, supports every harness Agentlog can
observe, deep-links to existing evidence, and can ship incrementally.

## Delivery sequence

1. Strict projection and tests.
2. Command Grid tokens, shell, loaders, and status language.
3. Live Command Floor and actor inspector.
4. Runtime lanes and sector topology on Overview.
5. Transcript, Sessions, and review surfaces.
6. Historical replay and original production sprite assets.

## Non-negotiable behavior

- No parser or database contract changes for visual convenience.
- No automatic edits to agent configuration.
- No remote transcript egress.
- No invented activity, specialty, lineage, success, or failure state.
- Loading preserves final layout and never uses the retired orbital capsule.
- Motion communicates observed work and stops when work stops.

