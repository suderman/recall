# Recall Specification

## 1. Purpose

Recall is a local-first personal history system.

Its job is to ingest evidence from many sources, preserve that evidence in source-native form when needed, normalize it into a common event model, resolve identities across systems, and later synthesize useful views such as daily journals, worklogs, timelines, and life reviews.

Recall is **not** just a journaling app and **not** just a worklog tool. It is a durable personal archive and reconstruction system.

## 2. Core principles

### 2.1 Capture first, interpret later
Raw evidence must be preserved before higher-level summaries are generated.

### 2.2 Local-first
The system should prefer local data, local storage, local processing, and explicit user-owned exports.

### 2.3 Replayable normalization
Normalization must be deterministic and rerunnable from raw artifacts.

### 2.4 Inspectability
Intermediate artifacts should remain easy to inspect manually.

### 2.5 Boring infrastructure
Prefer simple files, SQLite, explicit schemas, and thin services over clever distributed systems.

### 2.6 Source-native boundaries
Each connector should know its source well, but the core system must not become source-specific.

### 2.7 Provenance is mandatory
Every normalized event should retain a traceable path back to its raw source.

### 2.8 First-class artifacts
Attachments, files, images, and other source-native media are first-class evidence.

They must not be treated as incidental text decorations on messages.

Recall should preserve:
- source-native attachment/file metadata
- source object ids where available
- remote/provider locators
- linkage from events to artifacts
- optional local downloaded mirrors when policy allows

### 2.9 Recall is not a web archiver
Recall archives source-native evidence from the systems it connects to.
It does **not** crawl, snapshot, or archive arbitrary web pages just because a URL appears in text.

Rules:
- preserve pasted URLs as link metadata only
- do not fetch arbitrary external web pages for archival purposes
- only download provider-hosted or source-native attachment/file/media objects when explicitly in scope

## 3. Primary goals

Recall should eventually support:

- daily journal generation
- worklog reconstruction
- historical backfill
- cross-source life timeline views
- identity resolution across systems
- future import of export-only archives

## 4. Non-goals for early versions

Early versions should **not** try to:

- perform full people resolution automatically
- solve perfect summarization
- build a web product or SaaS
- over-model every future source before real data exists
- require a database server beyond SQLite
- hide raw data behind opaque abstractions

## 5. System layers

Recall has six layers.

### 5.1 Connectors
Source-specific capture or import logic.

Examples:
- Slack
- Telegram (TDLib)
- BlueBubbles
- notmuch
- khal / vdir calendars
- Asana
- future export imports (social media archives, etc.)

A connector may support one or both of:
- **capture**: incremental acquisition of new data
- **backfill**: historical import across a date range or archive

### 5.2 Raw evidence store
Source-native artifacts stored on disk.

Examples:
- Slack API capture artifacts
- Telegram raw message/update logs
- BlueBubbles webhook payload logs
- import ZIP extractions
- source-specific JSON dumps

Not every source needs a separate raw capture layer.
If the source is already local and authoritative, Recall may query it directly during synthesis or normalization.

Examples:
- notmuch
- khal / local vdir calendars

### 5.3 Artifact layer
Artifacts are first-class evidence objects associated with events.

Examples:
- Slack file objects
- Telegram photos, documents, voice notes, videos
- BlueBubbles/iMessage attachments
- email attachments
- files embedded in imported export bundles

Artifacts may be represented in two forms:
- metadata-only records
- metadata plus an optional local downloaded mirror

Artifacts augment the archive but do not replace raw source records.

### 5.4 Normalization
Transform raw or locally queried data into a common normalized event contract.

### 5.5 Entity resolution
Resolve identities, people, and later other entities such as organizations, projects, and places.

### 5.6 Derived views
Generate journals, worklogs, reports, and timeline views from normalized events plus entity resolution.

## 6. Canonical storage model

### 6.1 Raw data
Canonical for remote/event-driven sources.

Stored under:

```text
/data/raw/<source>/...
```

### 6.2 Normalized events
Canonical normalized representation should remain file-first and inspectable.

Stored as daily JSONL:

```text
/data/normalized/YYYY/YYYY-MM-DD.jsonl
```

This is the canonical normalized event store.

### 6.3 SQLite
SQLite is the operational database, not the canonical evidence store.

Use SQLite for:
- capture state / cursors
- indexes
- lookup tables
- identities
- people
- aliases
- resolutions
- artifact indexes and download state
- query acceleration
- later projections and caches

Do **not** let SQLite become the only place where important provenance or replay logic lives.

### 6.4 Artifact metadata and local mirrors
Artifacts are additive archive objects, not replacements for raw source payloads.

Recommended storage shape:

```text
/data/artifacts/
  metadata/
  blobs/
```

Guidelines:
- artifact metadata should remain inspectable and replay-safe
- local downloaded files/media should preserve stable linkage back to source-native object ids and parent event provenance
- local mirrors must not overwrite or replace original remote/source locators
- if bytes are not downloaded, artifact metadata may still exist with status such as `not_requested`, `deferred`, or `failed`

## 7. Normalized event contract

The normalized event model must be broad enough to support messages, emails, meetings, tasks, imported posts, and later other event types.

Minimum v1 fields:

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
  "sender_identity_id": "ident_slack_U024FEL01",
  "participant_identity_ids": [
    "ident_slack_U024FEL01",
    "ident_slack_U08LWQMG6S3"
  ],
  "text": "Downloadable Vimeo: https://example.com/post/123",
  "source_urls": [
    "https://example.com/post/123"
  ],
  "artifact_ids": [
    "artifact_slack_F024FEKMZ"
  ],
  "raw_ref": {
    "source": "slack",
    "path": "data/raw/slack/2026-03-31/messages.jsonl",
    "locator": {
      "channel": "C024FEKMZ",
      "ts": "1774978267.000000"
    }
  },
  "raw_fragment": null,
  "tags": ["message"]
}
```

Rules:
- normalized events must preserve source ids
- normalized events must preserve provenance
- normalized events may reference identities even when people are unresolved
- normalized events may reference zero or more first-class artifacts
- normalized events must preserve original message text
- normalized events must preserve original source-native URLs where relevant
- normalized events must not replace remote/source locators with local-only paths
- normalized events must remain replayable from raw input
- normalized events should be append-only for a given normalization run, with explicit rerun semantics

## 8. Artifact model and download policy

Artifacts are first-class evidence objects distinct from normalized events.

### 8.1 Purpose
Events describe what happened.
Artifacts describe attached or provider-native evidence objects related to what happened.

### 8.2 Artifact identity
Prefer, in order:
1. provider file/object id
2. parent event/message provenance
3. remote/source locator as fallback
4. content hash for verification or dedupe after download

Do not treat arbitrary pasted URLs as artifact identities.

### 8.3 Minimum artifact fields
A normalized artifact record should support at least:

```json
{
  "artifact_id": "artifact_slack_F024FEKMZ",
  "source": "slack",
  "account": "work",
  "kind": "file",
  "source_object_id": "F024FEKMZ",
  "event_ids": ["evt_..."],
  "remote_locators": [
    {
      "kind": "provider_url",
      "value": "https://files.slack.com/files-pri/..."
    }
  ],
  "local_path": null,
  "mime_type": "image/png",
  "filename": "diagram.png",
  "size_bytes": 482193,
  "checksums": {},
  "download_status": "not_requested",
  "observed_at": "2026-03-31T17:31:07Z",
  "raw_ref": {
    "source": "slack",
    "path": "data/raw/slack/2026-03-31/messages.jsonl",
    "locator": {
      "channel": "C024FEKMZ",
      "ts": "1774978267.000000",
      "file_id": "F024FEKMZ"
    }
  }
}
```

### 8.4 Source-native vs external URLs
In scope for artifact download:
- provider-hosted file/media objects that are part of the source system
- source-native attachment/file records
- files/media embedded in imported export bundles

Out of scope:
- arbitrary links pasted into text
- normal web pages
- articles, blogs, profiles, product pages, and other open-web destinations
- general web crawling or internet archiving

### 8.5 Raw immutability
Raw source files must remain unchanged after capture.

Do not:
- rewrite raw payloads
- replace remote locators with local paths
- strip attachment metadata out of raw records

### 8.6 Additive local mirrors
If Recall downloads source-native attachment bytes:
- preserve the original remote/source locator
- store local mirror metadata separately
- record download status explicitly
- record checksums when available
- treat local mirrors as additive archive material, not canonical replacements

### 8.7 Metadata-first policy
Implementation order should be:
1. artifact metadata capture
2. optional byte download
3. optional later extraction such as OCR or text extraction

### 8.8 Replayability
Artifact metadata should be reproducible from raw source evidence and explicit download policy.
Downloaded byte mirrors may be regenerated or restored independently of normalized event replay.

## 9. People and identity normalization

People and identities must be modeled separately.

### 9.1 Person
A canonical human being.

### 9.2 Identity
A source-specific handle, account, email, phone number, user ID, or address.

Examples:
- Slack user ID
- email address
- Telegram user ID
- BlueBubbles handle
- phone number
- social media account handle

### 9.3 Alias
A textual label or display name associated with a person.

### 9.4 Resolution
Evidence that a given identity belongs to a person.

### 9.5 Unresolved identities
These are valid and expected. Do not force weak matches.

Important rule:
**events should normalize identities first, not people.**

People resolution can improve later without re-ingesting raw events.

## 10. Connector categories

### 10.1 Remote pull connectors
Examples:
- Slack
- Asana

These usually capture data on demand or on a schedule and write raw artifacts.

### 10.2 Always-on capture connectors
Examples:
- Telegram via TDLib
- BlueBubbles via webhooks

These should append source-native events continuously and allow nightly synthesis later.

### 10.3 Local query connectors
Examples:
- notmuch
- khal / vdir

These do not necessarily need a separate raw capture layer because the authoritative data is already local.

### 10.4 Import connectors
Examples:
- downloaded archives from social networks
- export ZIPs
- historical dumps

These should import archives into raw storage and then normalize from there.

## 11. Current source strategy

### 11.1 Slack
- nightly pull
- raw artifacts on disk
- normalized daily events
- later incremental cursor/state in SQLite

### 11.2 Telegram
- always-on TDLib-backed capture service
- local persistent database
- append raw updates/messages
- normalize downstream

### 11.3 BlueBubbles
- always-on webhook receiver
- append raw events
- normalize downstream
- historical backfill via explicit export import, not webhook replay
- optional attachment byte preservation via export bundles, not remote crawling

### 11.4 Email
- query notmuch directly at synthesis or normalization time
- raw capture not required initially

### 11.5 Calendar
- query khal / local calendar data directly at synthesis or normalization time
- raw capture not required initially

### 11.6 Future import-only sources
- treat archives as first-class raw evidence
- normalize into the same event contract

## 12. CLI shape

Recall should remain CLI-first.

Intended command family:

```text
recall capture <source> ...
recall backfill <source> ...
recall import <source-export> ...
recall normalize <source> ...
recall events show ...
recall entities ...
recall journal build ...
```

Guidelines:
- CLI commands should do one thing clearly
- source-specific commands should live at the edge
- derived-view commands should consume normalized data, not call raw connectors directly
- export imports should preserve the original bundle untouched before normalization

## 13. Project layout

Preferred layout:

```text
src/recall/
  cli/
  connectors/
    slack/
    telegram/
    bluebubbles/
    email/
    calendar/
  normalize/
  entities/
  storage/
  synthesize/
  common/
config/
  sources/
data/
  raw/
  normalized/
  derived/
  state/
  artifacts/
tests/
examples/
```

## 14. Raw artifact conventions

Raw artifacts should be stable, source-native, and inspectable.

Examples:
- `metadata.json`
- `conversations.json`
- `messages.jsonl`
- `events.jsonl`
- `archive-manifest.json`

Rules:
- keep enough source detail to rerun normalization
- prefer JSON or JSONL for intermediate storage
- avoid lossy transformation during raw capture
- do not mix normalized fields into raw artifacts
- preserve source-native file or attachment objects exactly as captured
- do not rewrite raw attachment URLs to local paths
- do not remove provider payload details that may be needed for later download
- do not follow arbitrary external URLs found in text

## 15. Artifact download policy

Artifact download must be explicit and scoped.

Rules:
- metadata capture is the default
- byte download is optional and policy-controlled
- download policy applies only to source-native/provider-native attachments and media
- arbitrary web links are never implicit download targets
- download success or failure must be recorded explicitly
- lack of download must not block event normalization

Examples in scope:
- Slack file objects
- Telegram media/file objects
- BlueBubbles attachments
- email attachments
- files inside imported archives

Examples out of scope:
- pasted article URLs
- social profile URLs
- normal websites
- arbitrary external pages

## 16. State and incremental capture

Capture state belongs in SQLite.

Examples:
- last Slack cursor
- last Telegram update offset
- BlueBubbles receiver checkpoints
- import status of archive manifests

Rules:
- state should accelerate capture, not become a hidden source of truth
- backfill must remain possible even if state is reset
- raw artifact contracts should remain stable regardless of capture mode

## 17. Testing strategy

Recall should favor fixture-driven tests.

Priority areas:
- connector pagination behavior
- text normalization
- provenance preservation
- raw path generation
- normalized event output
- artifact metadata preservation
- artifact reference integrity
- identity extraction
- date partitioning
- replayability

Golden-file tests are appropriate for normalized JSONL output.

## 18. Development stack

Preferred stack:
- Python
- Typer for CLI
- FastAPI only where an always-on HTTP listener is needed
- SQLAlchemy Core
- SQLite
- Nix for environment reproducibility

Frameworks should stay at the edges.
The center of Recall should remain plain code, explicit schemas, and durable files.

## 19. Near-term roadmap

### Phase 1
Foundation and first connector.

### Phase 2
Slack raw capture and normalization.

### Phase 3
Identity extraction for Slack into entity tables.

### Phase 4
Incremental Slack capture state.

### Phase 5
Attachment metadata capture and artifact references.

### Phase 6
Optional source-native artifact download policy and storage.

### Phase 7
Local-query integrations for email and calendar.

### Phase 8
Second major connector: likely Telegram or BlueBubbles.

### Phase 9
Initial journal synthesis over normalized events plus identities.

### Phase 10
Historical backfill and import pipeline for old life data.

## 20. Decision rules for future work

When adding a new source, answer these questions first:

1. Is this source remote, always-on, local, or import-only?
2. Does it need a raw evidence layer, or is the source already local and authoritative?
3. What is the stable source-native unit to preserve?
4. How will provenance be represented in normalized events?
5. What identities does this source introduce?
6. Can this connector support both incremental capture and historical backfill?
7. What is the minimum useful vertical slice?
8. Does this source expose source-native attachments or media objects?
9. What counts as source-native artifact metadata versus arbitrary external links?
10. What is the explicit download policy boundary for this source?

## 21. Architectural guardrails

Do not:
- couple journal synthesis directly to source connectors
- let one source define the universal schema by accident
- hide provenance in opaque database-only state
- force premature people resolution
- normalize away important source-native ids
- design for every future connector before building the next real one
- mutate raw capture after the fact
- replace original source URLs with local-only paths
- silently treat pasted web links as artifact download targets
- drift into general internet archiving

Do:
- preserve raw evidence
- keep normalization replayable
- store identities explicitly
- keep artifacts inspectable
- build thin vertical slices
- generalize only after real data pressures the model
- preserve both remote locators and local mirrors when downloads occur
- model artifacts as first-class evidence
- keep download policy explicit and source-scoped

## 22. Definition of success

Recall is succeeding when:
- new sources can be added without distorting the core
- normalized daily events can be rebuilt from raw evidence
- source-native attachments and media can be preserved without turning Recall into a web archiver
- identities can be resolved over time without re-capturing sources
- daily journals and worklogs can be synthesized from normalized data
- historical backfill is possible, not just forward capture
- the system becomes a durable local second brain of lived experience
