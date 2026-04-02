# Recall

**Recall** is a local-first archive of my life.

It pulls in data from the systems I already use — messaging, email, calendars,
task tools, exports, the works — so I can figure out what happened on any given
day, build journals and worklogs, and keep continuity over time.

> capture first, interpret later.

Recall isn't just a fancy journal app. It's:

- daily journal entries
- worklogs and timesheets
- timelines
- monthly reviews
- searchable memory
- historical reconstruction

## Why bother

The problem is my data is:

- remote and transient
- hard to search later
- siloed by vendor
- only available through manual exports
- pretty bad at long-term narrative reconstruction

Recall brings all those sources into one local system I actually control.

## Design principles

### 1. Local-first

The archive lives on my own systems. No vendor lock-in.

### 2. Raw before derived

The primary artifact is not a summary. It's the captured evidence, as-is or
close to it.

### 3. Normalize everything

Different sources should converge into one common event model.

### 4. Journal as a view

A journal entry is derived output, not the source of truth.

### 5. Support both present and past

- ongoing capture
- historical backfill
- import from archived export data

### 6. Add sources without redesign

New connectors should plug into the same normalized event store. No
rearchitecture every time I add a new platform.

## High-level architecture

Three layers:

### Raw

Source-native data captured or imported as-is, or close to as-is.

- Slack daily capture JSON
- Telegram raw TDLib events
- BlueBubbles webhook event logs
- social media export archives
- vendor ZIP/JSON/CSV dumps

### Normalized

A common event store that hides source-specific differences. This is the center
of gravity — all downstream logic should prefer normalized events over
source-native data whenever possible.

### Derived

Outputs built from normalized events.

- daily journal files
- worklogs
- summaries
- monthly reports
- search indexes

## Repository layout

Starting point, not a prison:

```
recall/
  README.md
  AGENTS.md
  connectors/
    slack/
    telegram/
    bluebubbles/
    email/
    calendar/
  imports/
  raw/
    slack/
    telegram/
    bluebubbles/
    imports/
  normalized/
    2026/
      2026-03-31.jsonl
      2026-04-01.jsonl
  derived/
    journal/
      daily/
      weekly/
      monthly/
    worklog/
  scripts/
  docs/
  schemas/
```

## Normalized event store

Everything transforms toward a shared event model:

- what happened
- when it happened
- where it came from
- who was involved
- what text or metadata mattered
- how to get back to the original source if needed

```json
{
  "source": "slack",
  "account": "jon-work",
  "timestamp": "2026-03-31T17:31:07Z",
  "date": "2026-03-31",
  "kind": "message",
  "conversation_id": "C024FEKMZ",
  "conversation_label": "#webteam",
  "sender": "Jon",
  "participants": [],
  "text": "Downloadable Vimeo: ...",
  "thread_id": "1774987867.000000",
  "tags": [],
  "raw_ref": "raw/slack/2026-03-31.json",
  "raw": {}
}
```

The schema can evolve. The principle shouldn't.

## Source types

Three broad categories:

### 1. Ongoing remote sources

These need capture because the source isn't already local in durable form.

- Slack
- Telegram
- BlueBubbles
- future chat or social platforms

### 2. Local authoritative sources

These already live on my machine and can usually be queried directly during
synthesis.

- notmuch / Maildir
- khal / local calendar vdirs
- local notes
- git repos

These don't always need a separate raw capture layer.

### 3. Import-only or export-driven sources

These come from archives or one-off exports.

- social media export ZIPs
- CSV exports
- account history dumps
- backup folders

These should be importable without changing the overall architecture.

## Capture modes

Each connector can support one or both:

### Capture mode

Incremental collection for new data. Think nightly Slack pull, always-on
Telegram daemon, that sort of thing.

### Backfill mode

Historical reconstruction for past dates. Day-by-day history pulls, old export
parsing, rebuilding events for years I've already lived through.

Ideally a connector does both, even if backfill comes later.

## Initial planned sources

### Slack

Nightly batch capture using the Slack API and my user token.

### Telegram

Likely always-on capture using TDLib as my real Telegram client.

### BlueBubbles

Always-on webhook-based capture.

### Email

notmuch directly during synthesis — no extra raw layer needed since it's already
local.

### Calendar

khal / local calendar data directly during synthesis. Same deal.

### Asana

Import or query structured task data as needed.

## Workflow

### Ongoing

1. Capture or query source data.
2. Preserve raw evidence.
3. Normalize into daily event files.
4. Build derived views — journals, worklogs, summaries.

### Backfill

1. Pick a source and date range.
2. Pull or import historical data.
3. Normalize into the same event model.
4. Rebuild derived outputs if needed.

## What this is not

Recall isn't:

- a hosted SaaS product
- a vendor-locked notes app
- a summary-only system
- a magical black box that replaces evidence with prose

## Near-term goals

- establish repo structure
- define normalized event schema
- get Slack daily capture working first
- add journal synthesis over Slack + email + calendar
- design backfill/import conventions before the project sprawls

## Development

The initial implementation stack is:

- Python
- Typer for CLI commands
- FastAPI for always-on webhook or admin edges
- SQLAlchemy Core + SQLite for operational state and entity tables
- daily JSONL files as the canonical normalized event store

If you use Nix:

```bash
nix develop
```

If you use a local Python environment:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
```

Initialize the workspace and state database:

```bash
recall init
```

Configure Slack capture by copying `config/sources/slack.toml.example` to
`config/sources/slack.toml` and exporting the configured token environment
variable. Keep `artifact_download_policy = "metadata-only"` unless you
explicitly want Recall to download source-native Slack file objects.

Configure BlueBubbles capture by copying `config/sources/bluebubbles.toml.example`
to `config/sources/bluebubbles.toml`, then point the BlueBubbles server at the
Recall webhook URL on your LAN. Use the Recall host's reachable LAN IP, not
`0.0.0.0`, for example `http://10.1.0.6:8042/bluebubbles/webhook?token=...`.

To enable live BlueBubbles attachment downloads on `kit`, also set:
- `server_url` to the BlueBubbles server on `bub`
- `password_env_var` to an env var that contains the BlueBubbles server password
- `artifact_download_policy = "download-source-native"` if you want downloads by default

For historical BlueBubbles backfill, export a bundle on the Mac first, then
import it into Recall. The export bundle should contain `manifest.json` and
`messages.jsonl` at the top level. If you want attachment bytes copied into the
bundle too, pass `--include-attachment-bytes` during export.

Minimal macOS environment steps required to achieve BlueBubbles export:

```bash
# on the macOS machine that has the Messages history
curl -LsSf https://astral.sh/uv/install.sh | sh
source "$HOME/.local/bin/env"

# install a local Python 3.12 runtime without needing Nix or Xcode tools
uv python install 3.12

# copy the Recall repo onto the Mac, then from the repo root
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e .

# export a BlueBubbles-compatible bundle from the local Messages database
recall export bluebubbles-history ~/exports/recall/bluebubbles-history-YYYY-MM-DD_YYYY-MM-DD \
  --from YYYY-MM-DD \
  --to YYYY-MM-DD \
  --include-attachment-bytes
```

This path is intended for a minimal macOS export machine like an old MacBook.
It does not require Nix, Homebrew, or Xcode Command Line Tools for the
BlueBubbles history export flow.

Capture and normalize one day of Slack data:

```bash
recall capture slack --date 2026-03-31
recall capture slack --incremental --since 2026-03-31T00:00:00Z
recall normalize slack --date 2026-03-31
recall entities sync slack --date 2026-03-31
recall artifacts show --date 2026-03-31
recall artifacts download slack --date 2026-03-31 --dry-run
recall artifacts download slack --date 2026-03-31
recall capture bluebubbles serve
recall artifacts download bluebubbles --date 2026-03-31 --dry-run
recall artifacts download bluebubbles --date 2026-03-31
recall import bluebubbles-export /path/to/bluebubbles-export
recall normalize bluebubbles --date 2026-03-31
recall entities sync bluebubbles --date 2026-03-31
recall state show slack
recall events show --date 2026-03-31
```

Run the test suite:

```bash
python -m pytest
```

## Long-term goals

- full life history archive
- multi-source historical reconstruction
- robust backfill support
- importers for old export archives
- searchable personal timeline
- trustworthy daily, weekly, monthly, and yearly views

## Notes for future work

- Append-only raw data where possible.
- Keep normalized events auditable.
- Don't let summaries replace evidence.
- Source-native quirks live inside connectors, not scattered everywhere.
- Design for forgotten sources getting added later.
- A source that seems irrelevant today might matter for some earlier season of
  life.

## Related docs

- AGENTS.md — working rules, architecture constraints, connector expectations
  live here.
- Connector-specific setup notes — docs/ or inside each connector directory.

## Status

This is the very beginning.

First working piece is Slack daily capture. The bigger goal is a real second
brain built from the evidence of a life.
