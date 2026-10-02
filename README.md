# Recall

**Recall** is a local-first archive of my life.

It pulls in data from the systems I already use — messaging, email, calendars,
task tools, exports, the works — so I can figure out what happened on any given
day, build journals and worklogs, and keep continuity over time.

> capture first, interpret later.

Recall has two primary uses:

- Search history across messages, email, tasks, reminders, calendars, and other
  evidence to find things worth remembering.
- Derive readable daily journals with an LLM. Regenerate a day when the entry
  needs work or a better model becomes available, without changing its evidence.

Worklogs, reviews, and other views can use the same archive later.

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

```text
src/recall/
  cli/
  connectors/     # Slack, Telegram, BlueBubbles, email, calendar, Asana
  entities/
  normalize/
  storage/
  synthesize/
config/
  sources/
  entity-resolution/
data/
  raw/<source>/<capture-date>/
  normalized/<year>/<event-date>.jsonl
  artifacts/metadata/<source>/<year>/<date>.jsonl
  artifacts/blobs/
  derived/timelines/<year>/<date>.org
  derived/search.sqlite3
  derived/journal-failures/<date>/<attempt-hash>/
  derived/journal-inputs/<year>/<date>/<packet-hash>/
  derived/journals/<year>/<date>/<revision-hash>/journal.org
  state/recall.sqlite3
  state/rebuild/
  state/backfill/slack.jsonl
tests/
examples/
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
  "event_id": "evt_example",
  "source": "slack",
  "account": "jon-work",
  "timestamp": "2026-03-31T17:31:07Z",
  "date": "2026-03-31",
  "kind": "message",
  "conversation_id": "C024FEKMZ",
  "conversation_label": "#webteam",
  "sender_identity_id": "ident_slack_user_example",
  "sender_person_id": null,
  "participant_identity_ids": [],
  "participant_person_ids": [],
  "text": "Downloadable Vimeo: ...",
  "thread_id": "1774978267.000000",
  "tags": [],
  "artifact_ids": [],
  "source_urls": [],
  "raw_ref": {
    "source": "slack",
    "path": "data/raw/slack/2026-03-31/messages.jsonl",
    "locator": {"channel": "C024FEKMZ", "ts": "1774978267.000000"}
  },
  "raw_fragment": {}
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

## Implemented sources

### Slack

Nightly batch capture using the Slack API and my user token.

### Telegram

TDLib capture and daemon operation, plus Telegram Desktop export import.

### BlueBubbles

Webhook capture, bounded REST recovery after outages, and historical export import.

### Email

notmuch queries during normalization. The authoritative Maildir stays local.

### Calendar

khal queries during normalization, with explicit local-day timezone selection.

### Asana

JSON export import and task-event normalization. Direct API capture is not implemented.

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

- validate journal generation on more dates before nightly scheduling
- review generated journals for relevance and factual claims
- validate Telegram edge cases and artifact downloads
- validate bounded Slack backfill on a real workspace and extend other remote connectors
- assess conservative overlap deduplication without losing provenance

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
If `server_url` and the configured password environment variable are set,
Recall now attempts a bounded short-gap recovery on startup before the webhook
receiver begins serving.

Configure Telegram TDLib capture by copying `config/sources/telegram.toml.example`
to `config/sources/telegram.toml`, then export the configured environment
variables before running any `recall capture telegram tdlib-*` command.

Chat and user lookups use asynchronous TDLib requests. Updates received during
authorization, lookups, or file downloads are committed to a private per-account
`pending.sqlite3` beside the TDLib database and files directories. One process
may use that account state at a time. Pending updates survive client shutdown
and replay in receipt order, with stable local IDs and receipt timestamps.
Enriched payloads are frozen before delivery to the capture writer.

Capture removes a receipt only after its raw JSONL record is flushed to disk
and the Recall cursor is saved. A retry after raw save checks the existing
record instead of appending it again. Conflicting records and corrupt queues
stop capture without dropping the pending receipt. All `tdlib-*` capture
commands read the stored cursor unless explicitly overridden. Local update IDs
are counters, not Telegram history-completeness markers.

This is at-least-once delivery for updates already committed to the pending
queue. It does not recover updates TDLib never delivered or prove complete
historical coverage. A crash during a raw append can leave a malformed final
JSONL line; capture then stops for repair while retaining the pending receipt.
Keep the queue together with its account state and archive. Do not copy test
TDLib state over canonical state or delete pending files to bypass an error.

Batch limits cap returned updates, not updates received while waiting for a
lookup. Historical catch-up can grow the pending queue beyond that cap. Use a
wall-clock limit for supervised tests. Lookup timeouts retain message text and
source IDs even when chat or user names are missing.

Drain an existing queue offline with an explicit root and batch limit:

```bash
recall capture telegram drain --root /path/to/copied-capture --max-updates 1000
```

This command requires no credentials or TDLib library. It saves receipts through
the same raw-write, cursor, and acknowledgement checks, then reports the remaining
count. Run it on a copy for inspection. It changes the selected queue and archive,
so preserve the original queue together with its raw records and cursor.

Unfrozen messages can use saved chat/user observations from the same account.
Their objects and observation times are embedded in `payload.saved_context`;
these labels do not establish a historical name or relationship. Frozen payloads
remain unchanged, and missing names stay unresolved. Use `recall rebuild` with
an explicit timezone to group late receipts by message day instead of receipt
day. An empty queue does not prove complete Telegram history.

Saved TDLib service content has deterministic text and type tags. These
representations are covered by synthetic fixtures:

| Content type | Text | Added tag |
| --- | --- | --- |
| `messageUnsupported` | `Telegram unsupported content (body unavailable)` | `unsupported` |
| `messageAnimatedEmoji` | The literal Unicode `emoji`, or `Telegram animated emoji (emoji unavailable)` when missing or empty | `animated_emoji` |
| `messageCall` | `Telegram call`, or `Telegram audio call` / `Telegram video call` when `is_video` is a boolean | `call` |
| `messageContactRegistered` | `Telegram contact registration notification` | `contact_registered` |

Calls append `; duration: N seconds` only for a saved nonnegative integer,
including zero. Missing or invalid duration adds nothing. A saved nonempty
`discard_reason.@type` appends `; discard reason: TYPE` with its literal value.
Neither duration nor discard reason proves who answered or whether the call
succeeded. Contact registration does not establish when people met or first
spoke. Unsupported content cannot recover a missing body.

These events remain `kind="message"`, with `message` and `telegram` tags and
physical raw references. Animated emoji do not create artifact records or
trigger downloads. Existing media captions, including empty captions, are
unchanged.

Telegram TDLib setup steps:

1. Create or sign into a Telegram account with the phone number you want Recall
   to capture.
2. Open `https://my.telegram.org`, sign in with that phone number, then open
   `API development tools`.
3. Create an application there if you have not already. Telegram will show you
   an `api_id` and `api_hash` for that app.
4. Export those values into your shell as `TELEGRAM_API_ID` and
   `TELEGRAM_API_HASH`.
5. Export the same login phone number as `TELEGRAM_PHONE_NUMBER` in
   international format, for example `+15551234567`.
6. On first login, TDLib will ask Telegram to send a one-time login code.
   Recall prompts for that code interactively in the terminal by default, so
   `TELEGRAM_AUTH_CODE` is optional for normal manual runs.
7. If the Telegram account has two-step verification enabled, also export the
   password as `TELEGRAM_AUTH_PASSWORD`, or enter it when Recall prompts.
8. Make sure Recall can find `libtdjson`. In the Nix dev shell this is exported
   automatically as `TELEGRAM_TDLIB_LIBRARY_PATH`. Outside Nix, either export
   that env var yourself or set `tdlib_library_path` in
   `config/sources/telegram.toml`.

Example shell setup:

```bash
export TELEGRAM_API_ID="12345678"
export TELEGRAM_API_HASH="your-telegram-api-hash"
export TELEGRAM_PHONE_NUMBER="+15551234567"

# optional: preseed the first-login code for unattended runs
export TELEGRAM_AUTH_CODE="12345"

# only needed if Telegram two-step verification is enabled
export TELEGRAM_AUTH_PASSWORD="your-telegram-password"
```

If you are using `nix develop`, you should not need to set
`TELEGRAM_TDLIB_LIBRARY_PATH` manually. The shell now exports it for you.

Once those are set, start the daemon with:

```bash
nix develop -c recall capture telegram tdlib-daemon
```

Useful Telegram capture commands:

```bash
nix develop -c recall capture telegram tdlib-once
nix develop -c recall capture telegram tdlib-run --max-updates 50
nix develop -c recall capture telegram tdlib-daemon
nix develop -c recall state show telegram
```

After capture writes raw updates, normalize and sync identities with:

```bash
nix develop -c recall normalize telegram --date YYYY-MM-DD
nix develop -c recall entities sync telegram --date YYYY-MM-DD
```

To keep Telegram capture running outside an interactive shell, use the sample
systemd user unit at `examples/systemd/recall-telegram-tdlib.service`.

```bash
mkdir -p ~/.config/systemd/user ~/.config/recall
cp examples/systemd/recall-telegram-tdlib.service ~/.config/systemd/user/
```

Create `~/.config/recall/telegram.env` with your Telegram values:

```bash
TELEGRAM_API_ID=12345678
TELEGRAM_API_HASH=your-telegram-api-hash
TELEGRAM_PHONE_NUMBER=+15551234567
```

Then enable and follow the service:

```bash
systemctl --user daemon-reload
systemctl --user enable --now recall-telegram-tdlib.service
journalctl --user -u recall-telegram-tdlib.service -f
```

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
recall capture bluebubbles recover
recall import bluebubbles-export /path/to/bluebubbles-export
recall normalize bluebubbles --date 2026-03-31
recall entities sync bluebubbles --date 2026-03-31
recall state show bluebubbles
recall state show slack
recall events show --date 2026-03-31
```

Telegram artifact downloads copy available local files without starting TDLib.
TDLib fallback requires a retained raw reference to a native capture record for
the same account. Missing export files and unproved source records become
`not_available`; synthetic export file keys never reach numeric or remote-ID
fallback. Dry runs do not initialize TDLib or write metadata or blobs.

## Inspect repeated source records

```bash
recall events overlaps --root /path/to/archive
recall events overlaps --root /path/to/archive --source telegram --account personal
```

This read-only command scans saved Telegram and BlueBubbles daily raw logs. JSON
output groups repeated source/account/conversation/message keys, with every
physical raw-file line, capture mode, import ID, and SHA-256 hashes of the envelope
and message payload. Differing payloads remain separate observations. Missing
message keys are listed separately; the command never fills them from text or
receipt time.

BlueBubbles preserves native GUIDs across live capture and imports. Telegram
exports stay in a separate namespace for each import bundle. Their keys can be
synthesized by the importer, so repeated export keys are candidates, not proof of
matching native message IDs. The command does not match exports to TDLib, match
across sources, rewrite normalized events, or create a database. Normalized
upsert still keeps one selected raw reference per event, not a join ledger.

Telegram normalization keeps native event and artifact IDs unchanged. Imported
event IDs hash the compact ASCII JSON array
`["telegram-export-v1", account, import_id, str(chat_id), str(message_id)]`.
Imported artifact IDs hash
`["telegram-export-artifact-v1", event_id, source_object_id]`. Both use SHA-256's
first 20 hex characters with the existing `evt_` and `artifact_` prefixes. The
normalizer uses the retained import ID, not a moved export path, and rejects
missing, non-string or blank import IDs before publishing records. This separates
bundles and native messages without claiming Desktop/TDLib equivalence.

The `normalization_collisions` list reports shared current event IDs across audit
namespaces, not legacy IDs already in storage. It includes every observation under
each namespace. These warnings do not prove that messages are equivalent. The
read-only command does not change IDs or migrate stored records.

Replay old imports into a fresh workspace before building a new index or journal
packet. In-place normalization can leave old event and artifact IDs behind. New
import IDs must not inherit old acquired bytes or checksums solely through an old
ID match. Preserve old artifacts, packets and journals; ID isolation does not
rewrite historical citations or migrate canonical data.

Isolated rebuilds process capture days in sorted order and records in physical
line order. For one event ID on one output day, the last processed observation
wins. This is replay order, not a comparison of source edit timestamps. Rebuild
coverage counts distinct event IDs per day, even when several capture days
contain the same message. Search indexes the selected normalized record.

## Capture bounded Slack history

Choose the account label and date range before making a remote pull. The token
selects the Slack workspace; `--account` only labels the stored evidence. Source
settings and `.env` come from `--root`, with existing environment variables taking
precedence. Capture into a separate, non-overlapping output workspace:

```bash
nix develop -c recall backfill slack \
  --root "$PWD" --output-root "$HOME/recall-slack-raw" --account work \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton

nix develop -c recall rebuild \
  --root "$HOME/recall-slack-raw" --output-root "$HOME/recall-slack-replay" \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton \
  --source slack --account work
```

Dates are inclusive local days, including DST changes. Backfill preserves raw
attachment-only messages and scheduled details, without choosing journal content.
It filters stored timestamps to the requested day. Threads are expanded from
parents returned by that day's history query, so replies to older parents may
be missing. Archived conversations are excluded unless configured or selected
with `--include-archived`. Deleted messages, retention limits, and conversations
the token cannot access remain gaps. `captured` and `queried-empty` describe API
results, not complete life coverage.

Each completed daily directory has `metadata.json`, `conversations.json`,
`messages.jsonl`, and a hash checkpoint, `backfill.json`. The whole directory
publishes at once. Rerun the same command after failure or interruption; verified
days reuse their bytes without API calls. Changed, unowned, or conflicting
captures are refused, never replaced. Use a new output root for changed capture
options or a fresh pull. A writer lock prevents concurrent backfills there.

`data/state/backfill/slack.jsonl` records daily results and collection counts.
The command stops at the first failed day and exits nonzero; later days remain
unattempted until rerun. HTTP 429 retries respect `Retry-After`, with at most five
retries per request. Limited-history responses and missing pagination cursors
fail explicitly instead of claiming a complete page set.

Backfill does not alter source data, live cursors, entity resolutions, normalized
evidence, search indexes, or services. Review the separate replay, then include
it when rebuilding the search index. No cross-source fuzzy deduplication is added;
replayed Slack events retain their existing account/channel/timestamp IDs.

## Rebuild a week and inspect evidence

Rebuild from saved captures and selected local queries into a separate workspace:

```bash
nix develop -c recall rebuild \
  --root "$PWD" --output-root "$HOME/recall-week" \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton \
  --source bluebubbles --source slack --source telegram --source asana \
  --source email --source calendar

nix develop -c recall timeline build \
  --root "$HOME/recall-week" \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton
```

Open `~/recall-week/data/derived/timelines/2026/2026-03-31.org` in Emacs.
These are evidence timelines, not narrative journals or timesheets. They retain
source text, event IDs, identity IDs, raw locators, and links to available local
files. Calendar entries do not establish attendance. Missing input does not
establish that nothing happened.

Input and output roots must not overlap. The rebuild command does not pull
remote history, alter raw captures, reset cursors, or replace the input archive.
It scans every saved capture date for selected sources so late arrivals land on
their event day. The first run may process history outside the requested week.
Malformed input fails that source rather than silently publishing a partial
rebuild. Source-native attachment downloads are a separate operation.

Successful replacement is scoped to a source and account. Without `--account`,
all accounts for the selected source are in scope. Other sources stay intact.
Email and calendar queries use account `default`. `--account` filters source
account labels; it does not rename them.
Missing, unsupported, and failed inputs preserve the previous contribution;
successful rebuilds replace their scoped contribution. Empty removal requires
a successful local query or a proven empty capture.
Statuses describe saved/query evidence, not complete capture coverage.

`data/state/rebuild/manifest.jsonl` records each source/day result, options,
entity/configuration hashes, code hash, and query snapshot hash. Each job's
`input_manifest` points to `data/state/rebuild/inputs/<fingerprint>.jsonl`, which
stores the input-hash map once per source fingerprint instead of once per day.
This keeps long-range checkpoints small without losing provenance.
Raw inputs resume from hash-checked caches; email and calendar are queried again.
Rerun the same command after interruption. Keep raw evidence and checkpoint
files. Rewritten files are atomic, but the range is not one database transaction.
A single-writer lock prevents concurrent rebuilds in the same output workspace.

Timeline display timezone must match rebuild coverage. Rendering refuses to
run during a rebuild or overwrite edited/unowned timeline files. Keep handwritten
notes elsewhere. Imported text is fixed-width quoted text; Org blocks and Emacs
file-local variables are escaped. Timeline rendering does not call a model.
These reports are optional audit output, not the daily journal interface.

## Search saved history

```bash
nix develop -c recall search index --root "$PWD" --include-root "$HOME/recall-week"
nix develop -c recall search query "school" --from 2026-03-27 --to 2026-04-02
nix develop -c recall search query "client name" --org
nix develop -c recall search query "client name" --json
```

The local SQLite FTS5 index covers normalized evidence, not just journal prose.
It includes text, conversation titles, raw details, and observed identity labels.
Later included roots override exact repeated event records. Point events use
the winning root's day partitions. Calendar occurrences retain observed days
across roots, so a partial historical replay does not hide later overlapping days.
Date filters use stored partitions, not proof of attendance or current scheduling.
Labels are observations, not automatic person merges.

Queries match all supplied words. Results default to newest captured timestamps;
use `--relevance` for ranked text matches. Add `--source`, `--identity`, date
bounds, or `--limit` to narrow results. JSON includes full event records and
physical source references for an agent. Org output has quoted snippets and
links to normalized and available original evidence. Calendar locators remain
explicit where no local file link exists. The last captured record does not
prove the last real interaction.

Rebuild the index after changing normalized evidence. It is a snapshot, not a
live query of remote systems. Failed indexing preserves the previous database.
The index lives under `data/derived/`; it is not the primary event store. Org
notes and journal text can still be searched in Emacs. They are not imported
into this evidence index. There is no web UI or natural-language search service.

## Recall person or project context

```bash
nix develop -c recall search person "Alex" --json
nix develop -c recall search person --identity ident_email_alex_example_com --org
nix develop -c recall search project "Client project" --from 2026-01-01 --json
```

Person lookup matches words in observed identity names or addresses. It keeps
identities separate, returns first/latest captured evidence and bounded recent
history, and reports ambiguous names. Broad names show at most 20 candidates;
choose `--identity` to inspect one. A name found only in message text is returned
as an unlinked mention, never used to identify the sender or participant.

Replay retains labels for published identities under
`data/state/rebuild/identity-labels.jsonl`. The search index combines these with
read-only entity storage. It does not publish temporary resolution state or
merge people. Labels may describe reused addresses; inspect event-time evidence.
Earlier replay outputs need a refresh before their names enter the index.

Project lookup matches all supplied words in indexed evidence. It does not infer
project membership, current status, completion, or work hours. Both lookups accept
`--from`, `--to`, `--source`, `--limit`, `--org`, and `--json`. JSON carries complete
records and references for agents. Lookups reject a concurrent index replacement
rather than return mixed snapshots. They do not call a model or change evidence.

## Use Recall from an agent

The bundled `recall-history` skill routes questions about forgotten contacts,
past projects, and family obligations to the read-only lookup commands. Install
it from this checkout without replacing an existing skill:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/recall-history" "$HOME/.agents/skills/recall-history"
```

Pi discovers `~/.agents/skills` at startup. Start a new session or use `/reload`
after installation. Use `/skill:recall-history Who is Alex, and when did we last
hear from them?` to force the workflow. Automatic selection depends on the agent
loading the advertised skill; the skill does not intercept every question.

The bundled Bash wrapper resolves its checkout through the symlink, so it works
outside the repository and ignores an unrelated `RECALL_ROOT`. It uses the
checkout's existing Nix environment offline and only permits `query`, `person`,
and `project`, without archive/index overrides. It may write normal Nix caches.
It is command routing, not a security sandbox or a new model service.

The workflow asks the agent to inspect cited evidence, keep identity candidates
separate, and disclose stale or missing capture. Current obligations cannot be
confirmed from an old archive. Source instructions and credentials must not
be repeated or acted on. Missing setup/index errors are reported instead of
silently installing tools, capturing data, or rebuilding evidence. Existing Org
notes and journals remain separate search sources.

## Generate journals on demand

```bash
nix develop -c recall journal build \
  --root "$HOME/recall-week" --from 2026-03-27 --to 2026-04-02 \
  --author "Your name" --output "$HOME/org/journal" \
  --timezone America/Edmonton --model codex-lb/gpt-6.1-sol:medium
```

This uses the configured Pi provider route with tools, extensions, skills,
project context, and session persistence disabled. Only the journal instructions
and evidence packet enter the request. Remote transmission requires the user's
approval. The default route is `codex-lb/gpt-6.1-sol:medium`.

Entries publish under `~/org/journal/YYYY/MM/YYYY-MM-DD.org`. Completed days are
checkpointed; unchanged inputs/options reuse saved responses. `--regenerate`
requests new model output. Old revisions remain intact, and handwritten or
edited visible entries are never overwritten. No nightly service is installed.

Luna returned invented citation IDs and malformed markers in real tests. Sol
medium passed two real-data weeks and a second generation of a previously
failing day without changing the prompts or validation. This is a limited test,
not a guarantee. Invalid drafts are rejected, previous entries remain intact,
and the error points to a retained plain-text draft under
`data/derived/journal-failures/`. Citation membership is not a truth check; review
the prose too.
The selection policy favors people and milestones over routine account notices.

### Run capture, replay, and previews together

```bash
nix develop -c recall journal run \
  --root "$PWD" --workspace "$HOME/recall-runs/2026-03-30" \
  --from 2026-03-30 --to 2026-03-30 --author "Your name" \
  --source slack --source bluebubbles --source telegram \
  --source email --source calendar --source asana \
  --timezone America/Edmonton
```

This command replays selected sources from raw evidence and local queries, then
uses the same approved Pi route to generate previews. It does not use existing
normalized files as its replay input. Without `--capture-slack`, it makes no
remote capture calls and does not load Slack credentials. Email and calendar
queries still run against the local authoritative stores.

To include a bounded Slack API pull, add `--capture-slack` when starting a new
run. The account label and archived-conversation setting come from the source
configuration; `--slack-account` and `--include-archived` or `--exclude-archived`
override them. The account label does not select a workspace. Only Slack has
remote capture in this command. Other sources need existing capture/imports or
local query tools. Slack's token visibility, retention, and thread-window limits
still apply, and artifact bytes are not downloaded.

The dedicated workspace must not overlap the source root or its data/configuration
paths. It refuses unowned contents and nested workspace symlinks. `run.json` binds the input root,
dates, selected sources, timezone, and capture settings. Repeat the same command
to resume; choose a new workspace for a different scope. Existing capture,
replay, and model checkpoints handle retries. Local queries run again, and
changed evidence, author, or model can require new generation.

Results print capture limits and replay status before generation. Failed or
unsupported replay stops generation for the range; missing and empty sources
remain explicit coverage gaps. Newly captured Slack is a later exact-ID overlay
on the primary replay, so an earlier missing Slack capture can be followed by a
successful captured overlay. Days without evidence print `no-evidence` and skip
the model. An older preview is retained and reported, not presented as current.

Previews are always under `WORKSPACE/preview/YYYY/MM/YYYY-MM-DD.org`. Packets,
revisions, and generation state belong to `WORKSPACE/replay`; captured Slack raw
bundles and their normalized overlay belong to `WORKSPACE/capture` and
`WORKSPACE/slack-replay`. No canonical events, live cursors, search index, or main
journal entries are updated. Review a printed revision and use `journal publish
--root WORKSPACE/replay --revision REVISION --output "$HOME/org/journal"` to
publish it separately. No service or timer is installed.

### Review before publication

Use `journal build --output "$HOME/recall-preview"` to keep generated entries
away from the main journal while reviewing them. Each result also prints its
immutable `journal.org` revision path. Publish that revision without another
model request:

```bash
nix develop -c recall journal publish \
  --root "$HOME/recall-week" \
  --revision /path/to/immutable/revision/journal.org \
  --output "$HOME/org/journal"
```

To correct prose, copy the revision's `body.org` to a separate file and keep its
`[fn:EVENT_ID]` markers. Do not edit the immutable revision or the numbered
footnotes in a rendered journal. Pass the corrected body with
`journal publish --draft /path/to/reviewed-body.org` and the same options above.
Recall saves a new revision, retains the original model metadata, and records
the original revision and body hash in its review metadata. It does not claim
that the model wrote the corrected body or that the claims passed a truth check.

Publication checks the packet and all revision hashes, revalidates citation IDs
and Org safety, and refuses handwritten edits or a busy writer. Output must be
outside the primary and packet input roots' Recall data directories. Repeat
publication is safe; interrupted replacement can resume through its checkpoint.
For native Pi revisions with matching prompt/system hashes, the next unchanged
`journal build` reuses the reviewed body. Externally saved revisions lacking
those runner hashes can be published but do not seed the Pi generation cache.
Publication uses the frozen evidence packet, even if live evidence has since
changed. A later build prepares current evidence and regenerates when needed.

## Prepare and save a readable journal

Prepare the day's evidence in the replay workspace:

```bash
packet="$(nix develop -c recall journal prepare \
  --root "$HOME/recall-week" --date 2026-03-30 \
  --author "Your name" --timezone America/Edmonton)"
```

The packet contains a versioned `prompt.org`, all daily events in `events.jsonl`,
and hash-checked input/coverage metadata in `packet.json`. Preparation keeps every
normalized event, including routine alerts that the journal may omit. It does
not call a model or download anything. Keep these private files out of Git.

Both `journal prepare` and `journal build` accept repeatable `--include-root`
options for accepted normalized replays:

```bash
packet="$(nix develop -c recall journal prepare \
  --root "$HOME/recall-week" --include-root "$HOME/recall-slack-replay" \
  --date 2026-03-30 --author "Your name" --timezone America/Edmonton)"
```

Inputs follow command-line order. Later roots win only for identical event IDs;
missing daily files and empty replays do not remove earlier evidence. Repeated
IDs within one input file, invalid evidence, mismatched timezones, and active
replay writers stop preparation. Each root must have a normalized directory.
Included roots are read-only; packets and runner state stay in the primary root.

Multi-root packets record source-file and coverage hashes plus the winning
physical file/line for each event. Raw links resolve relative to that event's
root. Coverage follows source/account scope, so a work-account pull cannot certify
personal-account evidence. New evidence without matching coverage stays
unverified. An empty query conflicting with retained evidence reports partial
coverage rather than silently deleting events. This is an exact-ID overlay, not
fuzzy deduplication or canonical-store replacement. Changed replay inputs change
the packet identity and invalidate the runner cache.

Use the packet with an approved model to draft readable prose. The prompt asks
for a first-person account, related messages combined into conversations, and
unresolved commitments worth carrying forward. It treats source text as evidence,
not instructions, and distinguishes calendar plans from known activity. The on-demand runner uses this same packet/revision workflow.

Save the model's Org body in a separate draft file. Use `[fn:EVENT_ID]` citation
markers from the packet, with optional `**` sections, but no title, links,
footnote definitions, or executable Org directives. Then save a revision:

```bash
nix develop -c recall journal save \
  --root "$HOME/recall-week" --packet "$packet" \
  --draft /path/to/draft-body.org --model "actual-model-id" \
  --options '{"style":"first-person factual","temperature":0.2}'
```

Record the options actually used; omit settings that were not exposed. The
command prints the readable journal path. Consecutive event citations become
short numbered footnotes, with source links in a foldable Evidence section.
Model/input details remain in `generation.json`, outside the main reading text.

Each distinct revision gets its own directory. Repeating the same save returns
the same revision, and edited files are never overwritten. Later drafts can use
the same packet and a different model or prompt; changed prompts require a new
packet. Primary evidence and handwritten notes stay intact. Packet hashes and
citation membership are checked, not whether each narrative claim is true.
Review the journal before relying on it.

For checkout-source verification, rather than the packaged CLI:

```bash
nix develop -c env PYTHONPATH="$PWD/src" python -m recall.cli.main --help
nix develop -c pytest -q
nix develop -c ruff check src tests
nix build --no-link
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

- [AGENTS.md](AGENTS.md) records working rules and archive constraints.
- [SPEC.md](SPEC.md) describes event, artifact, and connector contracts.
- `config/sources/*.toml.example` and `examples/systemd/` have setup examples.

## Status

Recall has six normalization paths, local raw capture/import, SQLite identity
resolution, source-native artifacts, isolated date-range replay, and deterministic
Org evidence timelines. Local journal preparation and cited draft revision
storage are also available. Replay tests cover exact duplicate IDs, scoped stale
removal, download-state preservation, write failures, late arrivals, and local-day
boundaries including DST. Journal tests cover evidence integrity, short citations,
unsafe Org refusal, and preservation of prior or edited revisions.

Local full-text history search and on-demand Pi journal generation are available.
A bundled read-only skill connects people/project questions to cited lookup
commands in the user's agent workflow.
Sol medium is the journal default after passing two real-data weeks. A first-quarter
local replay also verified long-range checkpoints and expanded historical search.
Validation still fails closed and retains rejected drafts. Broader journal
validation, nightly scheduling, other remote range connectors, broad Telegram
validation, fuzzy overlap matching, and
worklogs remain unfinished.
