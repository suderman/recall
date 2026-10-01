# AGENTS.md

## Mission

Build **Recall**, a local-first personal archive that can reconstruct the user's life from many data sources over time.

This repository is not just for daily journaling. It is a long-lived system for:

- collecting raw evidence from communications, calendars, tasks, files, and later exports
- normalizing heterogeneous data into one durable event model
- resolving people, identities, and other entities across sources
- supporting both incremental capture and historical backfill
- generating derived views such as daily journals, worklogs, timelines, reviews, and search-friendly memory artifacts

The long-term goal is a **second brain of the user's life** built from data the user already owns, exports, or can capture locally.

The system must assume the user will keep remembering additional sources later.

---

## Core principles

### 1. The canonical asset is the normalized event store

Do **not** treat a daily journal entry as the source of truth.

The canonical source of truth is the **normalized event store** plus the raw imported/captured evidence behind it.

Journals, worklogs, summaries, and timelines are **derived views**.

### 2. Capture first, interpret later

For remote and ephemeral systems, prefer:

1. capture raw evidence
2. store it locally
3. normalize it
4. synthesize later

Do not collapse ingestion and summarization into one fragile step.

### 3. Local-first wherever possible

Prefer local storage, local processing, local formats, and replayable transformations.

Remote APIs are allowed for ingestion, but the long-term archive must belong to the user.

### 4. Every source must support future backfill

Even if a source starts as incremental-only, design it so it can later support historical recovery.

Every connector should conceptually support one or both of:

- `capture` — incremental ingestion of new data
- `backfill` — historical ingestion over a date range or export archive

### 5. Flexible source model

Assume the user will add more sources later, including:

- chat systems
- email
- calendars
- task systems
- exports from social media and web platforms
- notes systems
- browser history
- git history
- media libraries
- fitness / health / location logs
- one-off manual imports

Do not hardcode the architecture around today's sources.

### 6. Raw evidence should remain recoverable

When feasible, preserve raw source payloads or references to them.

Normalization should make the data useful, but not destroy provenance.

### 7. Derived outputs must be reproducible

A journal entry should be rebuildable from source data for the same date range.

### 8. Entities matter as much as events

Messages and events alone are not enough. The system must also normalize and resolve:

- people
- identities / handles / addresses
- organizations
- projects
- conversations / threads
- places

The first entity subsystem to build is **people + identities**.

---

## Current known sources

These are current known sources and how they should fit.

### Slack

- shape: remote API
- mode: **nightly pull**
- reason: bounded daily pull works well with the Slack API and user token model
- output: raw captured daily artifacts plus normalized events

### Telegram

- shape: client library / local client database via TDLib
- mode: **always-on capture daemon**
- reason: TDLib is stateful and is best treated like a real client with ordered updates and persistent local storage
- output: append-only raw events and normalized events

### BlueBubbles

- shape: webhook / event source
- mode: **always-on webhook receiver** with bounded REST recovery for short outages
- reason: event-driven ingestion is the natural fit, but short Recall downtime should be recoverable without manual export steps
- output: append-only raw events plus restart-friendly recovery pulls and normalized events

### Email via notmuch

- shape: already local, authoritative
- mode: **query directly at synthesis time**
- reason: no extra raw-capture layer is required because the system already owns the data locally
- output: normalized events and optional daily snapshots if useful

### Calendar via khal / local vdir / ics

- shape: already local, authoritative
- mode: **query directly at synthesis time**
- reason: no extra raw-capture layer is required because the system already owns the data locally
- output: normalized events and optional daily snapshots if useful

### Asana

- shape: API and/or export
- mode: import from export now, direct connector later if useful
- role: task evidence, completed work, worklog corroboration

### Future social / media exports

- shape: export archives, occasionally APIs
- mode: usually **import-only**
- role: historical backfill and personal timeline enrichment

---

## Progress checklist

Keep this section current as implementation advances.

### Done

- [x] Slack daily capture CLI with raw storage, normalization, artifact metadata, and artifact byte download support
- [x] BlueBubbles webhook capture, normalization, historical export import, and artifact byte download support
- [x] BlueBubbles short-outage recovery via REST message pull, cursor state, and CLI/state inspection
- [x] Telegram TDLib capture with append, once, run, and daemon commands
- [x] Telegram Desktop export import/backfill for extracted directories and zip bundles
- [x] Telegram normalization for text, photos, documents, voice notes, video, animation, audio, stickers, and video notes
- [x] Telegram artifact byte download support using local-path, file-id, and remote-id fallback through TDLib
- [x] SQLite-backed people, identities, aliases, identity aliases, and resolutions storage
- [x] Entity inspection CLI: `recall entities show people|identities|resolutions`
- [x] First-pass cross-source entity matching plus manual override config for identity resolutions and person merges
- [x] Temporal identity-resolution guidance documented for reused addresses and role accounts
- [x] Telegram auth prompting, quieter TDLib defaults, and systemd user service example

### Pending

- [x] Email/notmuch normalization and entity extraction
- [x] Calendar/khal normalization
- [x] Asana import and normalization
- [x] Timestamp-aware resolution application in normalization/enrichment using `valid_from` and `valid_to`
- [x] Suggested-match review workflow for ambiguous cross-source identities
- [x] CLI to inspect unresolved identities and proposed matches
- [ ] Broader Telegram validation with real-world forwards, service messages, channels, and large media/documents
- [ ] Telegram artifact coverage validation for real animation/video-note/sticker downloads
- [x] Isolated local date-range rebuild with scoped replacement, coverage, and resume checkpoints
- [x] Isolated Slack date-range backfill with hash-checked daily bundles and bounded rate-limit retries
- [ ] Real-workspace Slack backfill validation and remote orchestration across other connectors
- [ ] Deduplication logic across overlapping sources and imports
- [x] Deterministic cited daily Org evidence timelines with annotation protection
- [x] Hash-checked journal evidence/prompt packets and cited model-draft revision storage
- [x] On-demand Pi journal runner with checkpointing, revisions, and publication protection
- [x] Sol medium validation across two real-data weeks with preserved citations
- [x] First-quarter local replay and historical search validation with compact provenance
- [ ] Broader journal validation and nightly scheduling
- [x] Local SQLite full-text history index with readable, Org, and agent JSON results
- [x] Read-only person/project recall packets with separate identity candidates and cited history
- [x] Retained replay identity observations without publishing mutable resolution state
- [x] Discoverable read-only agent skill for cited people, project, and obligation lookup
- [ ] Worklogs and other derived views

---

## Recommended repository responsibilities

This repository should own:

- raw ingestion and raw import pipelines
- normalization into a canonical event store
- schemas and contracts
- connector-specific configuration and rules
- people and identity resolution
- journal / worklog / timeline synthesis
- provenance and deduplication logic
- date-range rebuild tooling

This repository should **not** start with:

- a complicated web UI
- premature dashboards
- aggressive ML pipelines
- cleverness that makes provenance impossible to inspect

Start with durable local files and clear command-line tools.

---

## Recommended repository layout

```text
.
├── AGENTS.md
├── README.md
├── docs/
│   ├── architecture.md
│   ├── event-schema.md
│   ├── entities.md
│   ├── source-connectors.md
│   ├── backfill.md
│   └── journaling.md
├── src/
│   ├── connectors/
│   │   ├── slack/
│   │   ├── telegram/
│   │   ├── bluebubbles/
│   │   ├── email/
│   │   ├── calendar/
│   │   ├── asana/
│   │   └── imports/
│   ├── normalize/
│   ├── entities/
│   ├── synthesize/
│   ├── dedupe/
│   ├── storage/
│   ├── cli/
│   └── common/
├── config/
│   ├── sources/
│   ├── tagging/
│   ├── journals/
│   ├── filters/
│   └── entity-resolution/
├── data/
│   ├── raw/
│   │   ├── slack/
│   │   ├── telegram/
│   │   ├── bluebubbles/
│   │   ├── imports/
│   │   └── snapshots/
│   ├── normalized/
│   │   └── YYYY/
│   ├── entities/
│   │   ├── persons.jsonl
│   │   ├── identities.jsonl
│   │   ├── aliases.jsonl
│   │   └── resolutions.jsonl
│   ├── derived/
│   │   ├── journals/
│   │   ├── worklogs/
│   │   ├── timelines/
│   │   └── indexes/
│   └── state/
│       ├── connectors/
│       ├── cursors/
│       └── dedupe/
├── scripts/
├── tests/
└── examples/
```

This layout is only a starting point. Keep the boundaries clear even if the final folder names change.

---

## Canonical normalized event store

### Why it matters

Every source must be transformed into a common event shape so higher-level tools do not need source-specific logic for every operation.

The system should normalize into a **date-addressable local store**.

A good default is:

- one JSONL file per day for normalized events
- grouped by year

Example:

```text
/data/normalized/2026/2026-03-31.jsonl
```

JSONL is a strong default because:

- append-only is easy
- incremental rebuilds are easy
- manual inspection is easy
- one bad event does not corrupt the whole file
- downstream indexing is straightforward

### Recommended normalized event shape

Every normalized event should preserve both normalized fields and source provenance.

A starting shape:

```json
{
  "event_id": "evt_...",
  "source": "slack",
  "account": "work",
  "timestamp": "2026-03-31T17:31:07Z",
  "date": "2026-03-31",
  "kind": "message",
  "conversation_id": "C024FEKMZ",
  "conversation_label": "#webteam",
  "thread_id": "1774987867.000000",
  "sender_person_id": "person_jon_suderman",
  "sender_identity_id": "ident_slack_U024FEL01",
  "participant_person_ids": ["person_jon_suderman", "person_ariel"],
  "participant_identity_ids": ["ident_slack_U024FEL01", "ident_slack_U08LWQMG6S3"],
  "title": null,
  "text": "Downloadable Vimeo: ...",
  "tags": ["work", "chat"],
  "links": [],
  "attachments": [],
  "location": null,
  "raw_ref": "data/raw/slack/2026-03-31/...json",
  "raw": {}
}
```

Not every field applies to every source. The important part is that the shape is stable enough for downstream synthesis.

### Event design rules

- preserve source identifiers
- preserve exact timestamps when available
- retain raw references or payloads when feasible
- include normalized text suitable for synthesis/search
- separate normalized person/entity references from raw source IDs
- never lose the ability to trace an event back to its origin

---

## People and identity normalization

This is a first-class subsystem.

A person may appear across sources as many different identifiers:

- Slack user IDs
- email addresses
- phone numbers
- Telegram user IDs
- BlueBubbles handles
- usernames on social platforms
- aliases and nicknames in export archives

Do **not** treat those source identifiers as the person.

Treat them as **identities attached to a person**.

### Person model

A person is the canonical human entity.

Example:

```json
{
  "person_id": "person_jon_suderman",
  "display_name": "Jon Suderman",
  "sort_name": "Suderman, Jon",
  "notes": "",
  "tags": ["self"],
  "created_at": "2026-03-31T00:00:00Z"
}
```

### Identity model

An identity is a source-specific handle or address.

The identity value itself should remain stable even when the human owner changes
over time. In other words, a reused email address or role account should remain
one identity value with time-sensitive person resolution layered on top.

Example:

```json
{
  "identity_id": "ident_slack_U024FEL01",
  "person_id": "person_jon_suderman",
  "source": "slack",
  "kind": "user_id",
  "value": "U024FEL01",
  "label": "Slack user ID",
  "is_primary": true,
  "status": "active",
  "valid_from": null,
  "valid_to": null
}
```

Another example:

```json
{
  "identity_id": "ident_email_jon_nonfiction_ca",
  "person_id": "person_jon_suderman",
  "source": "email",
  "kind": "email",
  "value": "jon@nonfiction.ca",
  "label": "Work email",
  "is_primary": false,
  "status": "active"
}
```

### Alias model

Aliases are observed names for the same person.

Examples:

- Jon
- Jonathan Suderman
- suderman
- @jon
- Jon S.

Example record:

```json
{
  "alias_id": "alias_jon",
  "person_id": "person_jon_suderman",
  "value": "Jon",
  "source": "manual"
}
```

### Resolution model

Resolutions record **how** an identity was linked to a person.

They should also be able to record **when** that linkage was valid.

Example:

```json
{
  "resolution_id": "res_001",
  "identity_id": "ident_slack_U024FEL01",
  "person_id": "person_jon_suderman",
  "confidence": "high",
  "method": "manual",
  "valid_from": null,
  "valid_to": null,
  "evidence": [
    "Matched known work email",
    "Observed self-authored Slack messages"
  ],
  "created_at": "2026-03-31T00:00:00Z"
}
```

### Identity resolution rules

- one person can have many identities
- one identity should usually map to one person at a given point in time
- unresolved identities are allowed and should be preserved
- names alone are not enough to collapse identities
- keep source-specific identifiers even after normalization
- identities may become stale, invalid, or reassigned over time
- event-time resolution matters for reused addresses, phones, and role accounts

Example edge case:

- `suderman@gmail.com` may resolve to one person for older mail and another
  person after a known handoff date
- a role address like `support@company.com` may map to different humans across
  employment periods

In those cases, preserve the identity value and store multiple dated
resolutions. Event normalization or later enrichment should select the
resolution whose validity window covers the event timestamp.

### Matching strategy

Use three levels:

#### 1. high confidence automatic

- exact previously-known Slack user ID
- exact previously-known email address, subject to date-bounded resolution rules
- exact previously-known Telegram user ID
- exact phone number already linked

#### 2. suggested

- strong name match plus contextual evidence
- same handle seen in multiple imported exports
- same phone or email plus known alias

#### 3. manual only

- ambiguous common names
- partial export records
- sparse historical data
- conflicting evidence

### Event design implication

Events should reference both normalized people and raw source identities.

Example:

```json
{
  "source": "slack",
  "timestamp": "2026-03-31T17:31:07Z",
  "sender_person_id": "person_jon_suderman",
  "sender_identity_id": "ident_slack_U024FEL01",
  "participant_person_ids": ["person_jon_suderman", "person_ariel"],
  "participant_identity_ids": ["ident_slack_U024FEL01", "ident_slack_U08LWQMG6S3"],
  "raw": {
    "user": "U024FEL01"
  }
}
```

This lets synthesis operate on people while audit/debugging can still trace back to the raw identity.

---

## Entities beyond people

People are the first entity type, but not the last.

Later entity types likely include:

- organizations
- projects
- conversations / threads
- places
- devices
- websites / services

Do not prematurely overbuild this. Start with people + identities, but keep the system general enough to add other entity types later.

---

## Connector model

Each source connector should fit into one or more of these modes:

### 1. live capture

Examples:

- Telegram via always-on TDLib
- BlueBubbles via webhooks

### 2. scheduled pull

Examples:

- Slack nightly capture
- periodic API fetchers

### 3. local query

Examples:

- notmuch
- khal / local vdir / ics

### 4. archive import

Examples:

- social media export ZIPs
- task CSVs
- one-off historical JSON or HTML exports

Each connector should ideally expose a common conceptual interface:

- `capture`
- `backfill`
- `import`
- `normalize`

Not every source needs all of them immediately, but the architecture should make room for them.

---

## Backfill strategy

Do not try to backfill everything at once.

Use passes.

### Pass 1 — structured and easy

- notmuch
- khal
- Slack
- Asana

### Pass 2 — messaging systems

- Telegram
- BlueBubbles / iMessage related history

### Pass 3 — exported archives

- social media
- cloud platforms
- one-off historical exports

### Pass 4 — weird/manual sources

- partial logs
- hand-curated imports
- anything brittle

Backfill should prefer **date-bounded, replayable, resumable** jobs.

---

## Synthesis model

The journal builder should **not** care deeply which source produced a fact.

It should operate over normalized events and entities.

A daily synthesis job should answer questions like:

- what happened today?
- who did the user interact with?
- what work was done?
- what meetings or appointments occurred?
- what personal events are worth preserving?
- what deserves mention in a worklog vs a life journal?

Derived outputs may include:

- daily journal entries
- worklogs
- weekly reviews
- monthly summaries
- personal timelines
- memory/search indexes

---

## Provenance, deduplication, and trustworthiness

### Provenance

Every normalized event should retain enough information to recover where it came from.

### Deduplication

Expect duplicates:

- same event seen in multiple systems
- imported archives overlapping with live capture
- repeated pulls of the same source window

Deduplication must be careful and explainable.

### Trustworthiness

Never silently merge uncertain person identities or duplicate events without preserving evidence.

When uncertain, preserve both and mark confidence.

---

## Implementation guidance

### Prefer simple durable storage first

Use files, JSONL, SQLite, or similarly boring local storage before fancy systems.

### Preserve replayability

A connector should be rerunnable for a date range without inventing a new architecture each time.

### Keep transformations inspectable

If a human cannot inspect how raw evidence became normalized data, the system is too opaque.

### Start with command-line tools

The CLI surface can come before any UI.

Examples:

- `recall capture slack --date 2026-03-31`
- `recall backfill slack --from 2026-01-01 --to 2026-03-31`
- `recall import archive twitter-export.zip`
- `recall journal build --date 2026-03-31`
- `recall entities resolve --source slack`

---

## What success looks like

A successful Recall system should eventually let the user:

- reconstruct a day from multiple data streams
- rebuild journals from improved logic later
- backfill older life periods from archives and exports
- query life history by date, person, project, or source
- connect the same human across Slack, email, Telegram, and old exports
- preserve both personal and work memory in one local-first system

If a design choice helps short-term convenience but weakens long-term replayability, provenance, or extensibility, choose the long-term design.
