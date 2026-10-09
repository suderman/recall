# Recall

Recall is a local-first personal archive. It collects messages, email, calendar
entries and task exports so I can search past events and turn saved evidence
into readable daily journals.

Raw evidence and daily normalized events are the archive. Journals, timelines
and the search index are derived views, not substitutes for that evidence.
Capture first, interpret later.

## What works

| Source | Available input |
| --- | --- |
| Slack | Daily or incremental API capture; bounded date-range backfill |
| Telegram | TDLib capture and daemon; offline pending-queue drain; Desktop collection exports |
| BlueBubbles | Webhooks; bounded REST outage recovery; Messages history export/import |
| Email | Local notmuch queries |
| Calendar | Local khal queries |
| Asana | JSON export import, not API capture |

Recall also has cross-source identity resolution, source-native attachment
metadata and optional byte downloads, isolated replay, SQLite full-text search,
and cited Org journal generation through Pi.

There is no web UI or installed nightly journal job. Worklogs, broader remote
backfill and fuzzy cross-source deduplication are not implemented. Capture and
real-world media coverage still need validation. A passing hash or citation
check does not prove complete history or a journal's factual claims.

## Get started

Requires Python 3.12 or newer. From this checkout, Nix supplies Python, CLI
dependencies, notmuch, khal and TDLib:

```bash
nix develop
recall init
recall --help
```

Without Nix, install the Python package in a virtual environment. Install
notmuch, khal or TDLib separately if using those connectors:

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e '.[dev]'
recall init
```

Commands use `--root`, then `RECALL_ROOT`, then the current directory as their
workspace. The Nix shell sets `RECALL_ROOT` to this checkout. Use explicit roots
when working with another archive.

Copy the needed [source config examples](config/sources/) to `.toml` files in
that workspace and set their credential environment variables. Keep
`artifact_download_policy = "metadata-only"` unless byte acquisition is intended.
For BlueBubbles, replace the example token and LAN settings before serving.
See [source setup](docs/usage.md#source-setup).

## Search saved history

Search needs normalized events and a built index. Indexing is explicit; queries
never fetch remote data:

```bash
recall search index --root /path/to/archive
recall search query "school" --root /path/to/archive --from 2026-03-27 --to 2026-04-02
recall search person "Alex" --root /path/to/archive --json
recall search project "Client project" --root /path/to/archive --org
recall search status --root /path/to/archive --json
```

Results cite saved evidence. Person lookup keeps ambiguous identities separate.
The index is a snapshot; inspect its date bounds and freshness before treating
an empty result as meaningful. See [search and recall](docs/usage.md#search-and-recall).

`recall voice candidates` reviews possible self-authored email passages with
explicit sender IDs and date bounds. Native Telegram plain-text review also
requires `--source telegram --account ACCOUNT`. Candidates remain ineligible.
`recall voice collect` can maintain private local samples under an explicit
ownership and original-writing policy; `recall voice inspect --require-current`
checks them against current saved evidence and policy. Email/Telegram normalization
and replay can opt in with `--voice-policy`; collection stays off by default.
Checked replay receipts support removed or empty contributions. Profiles and
voice-based rewriting are not implemented. See [candidate review](docs/usage.md#review-voice-candidates)
and [local collection](docs/usage.md#collect-local-voice-samples).

## Rebuild without changing the archive

Replay saved captures into a separate workspace. Email and calendar, when
selected, query their local stores. This command does not pull remote history:

```bash
recall rebuild --root /path/to/archive --output-root /path/to/replay \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton \
  --source slack --source bluebubbles --source telegram
```

Input and output roots must not overlap. Missing and failed sources remain
visible in coverage records. Review the replay before including it in an index
or journal. See [replay and backfill](docs/usage.md#replay-and-backfill).

## Make a journal preview

Journal generation uses the Pi SDK with a fixed single-request policy. Configure
`RECALL_NODE_EXECUTABLE` and `RECALL_PI_SDK` with absolute Node executable and SDK
entry-module paths, or pass `--node-executable` and `--pi-sdk`. Both generation
commands enforce an input byte budget. No alternate CLI runner exists.
It sends evidence to the selected provider, so approve transmission before running:

```bash
recall journal build --root /path/to/replay \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton \
  --author "Your name" --output /path/to/preview
```

The runner saves immutable cited revisions, resumes unchanged days and refuses
to overwrite edited entries. Review claims and omissions before using
`recall journal publish`. Preparation, inspection and publication need no model
request. See [journals](docs/usage.md#journals).

## Storage and development

Each workspace keeps raw captures in `data/raw/`, daily event JSONL in
`data/normalized/YYYY/`, attachment metadata and bytes in `data/artifacts/`,
derived views in `data/derived/`, and SQLite identities/cursors in
`data/state/recall.sqlite3`. Local Maildir and calendars remain authoritative.
Back up raw evidence, acquired bytes, state and accepted replay workspaces
alongside journals. Git ignores private data and config; that is not a backup.

Source lives in `src/recall/`; fixtures and regressions live in `tests/`.
Check this checkout explicitly rather than assuming the packaged CLI matches:

```bash
PYTHONPATH="$PWD/src" python -m recall.cli.main --help
pytest -q
ruff check src tests
ruff format --check src tests
```

`nix build --no-link` builds the standalone CLI package. Deployment examples in
[examples/systemd/](examples/systemd/) need host-specific paths, persistence
and credentials checked before use.

Detailed commands and safety limits live in the [usage guide](docs/usage.md).
Project contracts, working rules, TODOs and validation progress live outside
this repository in `~/org/work/suderman/recall/recall.org` and its linked
`contracts.org`.
