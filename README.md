# Recall

**Recall** is a local-first archive of my life.

It pulls in data from the systems I already use — messaging, email, calendars,
task tools, exports, the works — so I can figure out what happened on any given
day, build journals and worklogs, and keep continuity over time.

> capture first, interpret later.

The longer-term outputs are:

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
  state/recall.sqlite3
  state/rebuild/
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

- review real daily evidence timelines in Emacs
- validate Telegram edge cases and artifact downloads
- add bounded remote backfill using the existing local replay contract
- assess conservative overlap deduplication without losing provenance
- add narrative journals and worklog filtering only after evidence views are useful

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

## Rebuild a week and read it in Emacs

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
input hashes, entity/configuration hashes, code hash, and query snapshot hash.
Raw inputs resume from hash-checked caches; email and calendar are queried again.
Rerun the same command after interruption. Keep raw evidence and checkpoint
files. Rewritten files are atomic, but the range is not one database transaction.
A single-writer lock prevents concurrent rebuilds in the same output workspace.

Timeline display timezone must match rebuild coverage. Rendering refuses to
run during a rebuild or overwrite edited/unowned timeline files. Keep handwritten
notes elsewhere. Imported text is fixed-width quoted text; Org blocks and Emacs
file-local variables are escaped. No model service is used for synthesis.

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
Org evidence timelines. Replay tests cover exact duplicate IDs, scoped stale
removal, download-state preservation, write failures, late arrivals, and local-day
boundaries including DST.

Remote range backfill, broad Telegram validation, fuzzy overlap matching,
narrative journals, worklogs, and search indexes remain unfinished.
