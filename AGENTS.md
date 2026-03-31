# AGENTS.md

## Mission

Build a **local-first personal event archive and journal system** that can reconstruct the user's life from many data sources over time.

This repository is **not** just for daily journaling. It is a long-lived system for:

- collecting raw evidence from communications, calendars, tasks, and later exports
- normalizing heterogeneous data into one durable event model
- supporting both incremental capture and historical backfill
- generating derived views such as daily journals, worklogs, timelines, weekly reviews, and search-friendly memory artifacts

The long-term goal is a **second brain of the user's life** built from data the user already owns, exports, or can capture locally.

The system must assume the user will keep remembering additional sources later.

---

## Core principles

### 1. The canonical asset is the normalized event store

Do **not** treat a daily journal entry as the source of truth.

The canonical source of truth is the **normalized event store** plus the raw imported/captured evidence behind it.

Journals, worklogs, summaries, and timelines are **derived views**.

This matters because:

- parsers will improve over time
- prompts and synthesis logic will improve over time
- backfills will happen later
- the user may want multiple views of the same life data
- the user may want to rebuild old journal entries from better logic later

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
- fitness/health/location logs
- one-off manual imports

Do not hardcode the architecture around today's sources.

### 6. Raw evidence should remain recoverable

When feasible, preserve raw source payloads or references to them.

Normalization should make the data useful, but not destroy provenance.

### 7. Derived outputs must be reproducible

A journal entry should be rebuildable from source data for the same date range.

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

- shape: webhook/event source
- mode: **always-on webhook receiver**
- reason: event-driven ingestion is the natural fit
- output: append-only raw events and normalized events

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

### Future social/media exports

- shape: export archives, occasionally APIs
- mode: usually **import-only**
- role: historical backfill and personal timeline enrichment

---

## Recommended repository responsibilities

This repository should own:

- raw ingestion and raw import pipelines
- normalization into a canonical event store
- schemas and contracts
- connector-specific configuration and rules
- journal/worklog/timeline synthesis
- provenance and deduplication logic
- date-range rebuild tooling

This repository should **not** start with:

- a complicated web UI
- premature dashboards
- aggressive machine learning pipelines
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
│   ├── synthesize/
│   ├── dedupe/
│   ├── storage/
│   ├── cli/
│   └── common/
├── config/
│   ├── sources/
│   │   ├── slack.json
│   │   ├── telegram.json
│   │   ├── bluebubbles.json
│   │   ├── email.json
│   │   ├── calendar.json
│   │   └── imports.json
│   ├── tagging/
│   ├── journals/
│   └── filters/
├── data/
│   ├── raw/
│   │   ├── slack/
│   │   ├── telegram/
│   │   ├── bluebubbles/
│   │   ├── imports/
│   │   └── snapshots/
│   ├── normalized/
│   │   └── YYYY/
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

### Canonical normalized event schema

Every normalized event should include at least these fields.

```json
{
  "event_id": "stable-id",
  "source": "slack",
  "source_account": "work-slack",
  "source_event_id": "raw-source-id-or-compound-key",
  "source_type": "message",
  "captured_at": "2026-03-31T23:59:59Z",
  "occurred_at": "2026-03-31T17:31:07Z",
  "date": "2026-03-31",
  "timezone": "America/Vancouver",
  "actor": {
    "id": "source-specific-id",
    "name": "Jon"
  },
  "conversation": {
    "id": "conversation-id",
    "label": "#webteam",
    "kind": "public_channel"
  },
  "participants": [
    { "id": "...", "name": "..." }
  ],
  "title": null,
  "text": "Downloadable Vimeo: ...",
  "summary_hint": null,
  "thread_id": "thread-or-parent-id",
  "parent_event_id": null,
  "tags": ["work", "chat"],
  "facets": {
    "message": true,
    "meeting": false,
    "email": false,
    "task": false,
    "imported": false
  },
  "links": {
    "url": null,
    "permalink": null
  },
  "provenance": {
    "raw_path": "data/raw/slack/2026-03-31.json",
    "raw_pointer": "records[12].messages[3]",
    "import_batch": null,
    "normalized_by": "connector-name-or-version"
  },
  "raw_ref": {
    "source": "slack",
    "team_id": "T...",
    "channel_id": "C...",
    "ts": "1774987867.000000"
  }
}
```

Not every field will apply to every source, but the top-level shape should remain stable.

### Stable IDs

Every normalized event needs a deterministic `event_id`.

Prefer an ID derived from:

- source
- account
- native object id or timestamp
- context like thread or message id when needed

The ID must be stable across re-imports and rebuilds.

### Required guarantees

Normalized events must preserve:

- when it happened
- which source produced it
- who acted or sent it
- where it happened
- where to find the raw evidence again

If one of those is impossible for a source, record the limitation explicitly.

---

## Raw data policy

### Keep raw where it matters

For sources that are remote, ephemeral, or hard to reacquire, keep raw payloads locally.

Examples:

- Slack daily captures
- BlueBubbles webhook payloads
- Telegram TDLib event logs or source snapshots
- exported archives from third-party platforms

### Do not duplicate local source-of-truth stores without reason

For sources the user already owns locally, prefer direct queries at synthesis time rather than extra raw capture.

Examples:

- notmuch email store
- local calendar vdirs / ics via khal

Optional daily snapshots are allowed if they improve reproducibility, but they are not required.

---

## Connector model

Every connector should aim to expose a predictable interface, even if the implementation differs.

Conceptual operations:

- `capture` — ingest recent or new data
- `backfill` — ingest historical data for a date range or import archive
- `normalize` — convert raw material into canonical normalized events
- `verify` — validate health and detect auth/scope/config issues

Examples:

- Slack:
  - `capture day`
  - `backfill range`
- Telegram:
  - `daemon`
  - `backfill range`
- BlueBubbles:
  - `serve webhooks`
  - `backfill if feasible`
- Imports:
  - `import archive`
  - `normalize import`

### Connector outputs

A connector should document:

- what it captures
- what it cannot see
- whether it is incremental, archival, or both
- what raw artifacts it writes
- how it produces stable IDs
- whether it supports backfill
- known data-loss limitations

---

## Import-only and export-based sources

Many later sources will arrive as exports the user requests manually.

The architecture must treat import jobs as first-class, not as hacks.

Examples:

- social media account exports
- messaging exports
- platform activity archives
- downloaded CSVs
- zip archives
- manual text dumps

### Import rules

Importers must:

- preserve the original archive or source file path
- record the import batch
- avoid mutating original export files
- normalize imported records into the same event store
- record source-specific limitations

A generic import batch record should include:

- import id
- source platform
- archive path
- import timestamp
- date range covered if known
- normalization version

---

## Deduplication policy

The user will eventually ingest overlapping sources.

Examples:

- a Slack message may later appear in an export
- an email may also be referenced in a task system
- a meeting may appear in both calendar and chat discussion
- the same Telegram content may show up through both live capture and backfill

### Rules

- Do not dedupe aggressively at raw-ingestion time.
- Normalize first.
- Deduplicate at the normalized-event layer with explicit rules.
- Preserve provenance even when deduping.

Potential dedupe strategies:

- same source + same native id
- same conversation + same timestamp + same actor + same text hash
- import replay with identical raw refs

Never silently destroy source provenance.

---

## Journal and derived outputs

Derived artifacts may include:

- daily journal entries
- worklogs
- weekly reviews
- monthly summaries
- timeline exports
- searchable indexes
- memory prompts for other systems

### Daily journal

A daily journal should combine:

- communication evidence
- meetings/events
- email activity
- task completions or updates
- optionally personal events and context

A daily journal is a **narrative view**, not a replacement for the event store.

### Worklog

A worklog is a work-focused derived view and may emphasize:

- tasks worked on
- meetings
- messages indicative of action/completion
- task system state changes
- rough hour estimates or logged hours

### Future views

The repository should make it easy to answer questions like:

- what happened on a given date
- what work was done for a client this month
- what did the user do during a past trip or season of life
- what conversations surrounded a major event

---

## Recommended storage and file conventions

### Raw storage

Use source-specific raw folders.

Examples:

```text
/data/raw/slack/2026-03-31.json
/data/raw/telegram/2026/2026-03-31.jsonl
/data/raw/bluebubbles/2026/2026-03-31.jsonl
/data/raw/imports/twitter-archive-2020-2022.zip
```

### Normalized storage

Prefer year/day partitioning:

```text
/data/normalized/2026/2026-03-31.jsonl
```

### Derived storage

Derived material should be clearly marked as derived.

```text
/data/derived/journals/daily/2026-03-31.org
/data/derived/worklogs/2026-03-worklog.org
/data/derived/timelines/2026-Q1.json
```

### State storage

Keep connector cursors, checkpoints, and caches separate from canonical data.

```text
/data/state/connectors/slack.json
/data/state/connectors/telegram.json
/data/state/dedupe/index.sqlite
```

---

## Time handling

Time handling must be explicit and consistent.

### Rules

- Store timestamps in ISO 8601 when normalized.
- Preserve source-native timestamps in raw references.
- Also compute a local `date` field in the user's timezone.
- Default timezone: `America/Vancouver` unless source-specific reasons require something else.
- Distinguish clearly between:
  - occurred time
  - captured time
  - imported time

Never rely on implicit local time in normalized artifacts.

---

## Privacy and sensitivity

This repository will eventually describe a large portion of the user's life.

Treat it as highly sensitive local data.

### Rules

- prefer local storage only
- do not assume cloud sync by default
- avoid sharing raw archives casually
- design for selective export later
- keep secrets/tokens/config separate from repo-tracked code

Do not build features that require unnecessary third-party services.

---

## Configuration philosophy

Configuration must support new sources without codebase chaos.

### Recommended config categories

- source credentials and source-specific paths
- inclusion/exclusion filters
- tagging and heuristics
- journal synthesis rules
- backfill ranges and import metadata

The user will likely want per-source filters such as:

- ignore some channels or chats
- mark some sources as personal/work/family
- distinguish quiet background events from important ones
- suppress known noise sources

---

## Initial implementation priorities

### Phase 1: foundation

- create the repo
- define normalized event schema
- define raw vs normalized vs derived storage layout
- implement a minimal CLI skeleton
- document connector contracts

### Phase 2: current live sources

- Slack nightly capture
- BlueBubbles always-on capture
- Telegram always-on TDLib capture
- direct notmuch queries during journal synthesis
- direct calendar queries during journal synthesis

### Phase 3: normalization and synthesis

- normalize Slack/Telegram/BlueBubbles into canonical events
- synthesize daily journal entries
- synthesize worklogs where useful
- support rebuild for a single day or date range

### Phase 4: backfill

- support historical Slack range import
- support historical Telegram import where feasible
- support import-only archives
- support Asana historical task ingestion

### Phase 5: broader life archive

- import social/media exports
- add search and timeline views
- add memory-oriented summaries and thematic indexes

---

## Operational guidance for agents

When working in this repository:

### Do

- protect the canonical distinction between raw, normalized, and derived data
- preserve provenance
- prefer simple local files over fragile abstractions
- design for replayability and backfill
- keep source-specific logic inside connectors
- keep normalized schema stable and documented
- make rebuilds deterministic where possible
- document limitations honestly

### Do not

- treat journals as source-of-truth
- hide data-loss in summarization
- make schema changes casually
- overfit the system to one source
- build a huge UI before the archive model is stable
- destroy raw evidence once normalized
- assume today's connectors are the final set

---

## Definition of done for this repository

A mature version of this repository should make the following possible:

- ingest new daily activity from multiple sources
- import old activity from exports or historical pulls
- normalize all of it into one durable local event archive
- rebuild a journal for any given day from source evidence
- derive worklogs and timelines from the same base archive
- keep source provenance inspectable
- add new sources without redesigning the whole system

That is the real target: a **local, extensible, replayable life archive** that can become the backbone of a true second brain.
