# Using Recall

[README](../README.md) covers the short path. This guide covers connector setup,
replay, inspection and journal review. Run commands from a configured Recall
environment and substitute your own roots, dates and timezone.

Use `--root` explicitly when switching workspaces. It overrides `RECALL_ROOT`
and the current directory. Source settings belong in `ROOT/config/sources/`.
Keep credentials and archive files private. Remote capture, byte downloads and
model requests are separate operations; local inspection does not authorize them.

## Source setup

### Slack

Copy [slack.toml.example](../config/sources/slack.toml.example) to
`config/sources/slack.toml`. Export the configured token variable, normally
`SLACK_USER_TOKEN`. The token selects the Slack workspace; `account` labels saved
evidence and does not select an account remotely.

```bash
recall capture slack --root /path/to/archive --date 2026-03-31
recall capture slack --root /path/to/archive --incremental --since 2026-03-31T00:00:00Z
recall normalize slack --root /path/to/archive --date 2026-03-31
recall entities sync slack --root /path/to/archive --date 2026-03-31
recall state show slack --root /path/to/archive
```

Capture saves raw API evidence. Normalization and identity sync are separate.
Token visibility, deleted messages and retention limits constrain history.
No Slack schedule is installed by these commands.

### Telegram

Copy [telegram.toml.example](../config/sources/telegram.toml.example) to
`config/sources/telegram.toml`. Get an application's `api_id` and `api_hash` from
[my.telegram.org](https://my.telegram.org), then export `TELEGRAM_API_ID`,
`TELEGRAM_API_HASH` and `TELEGRAM_PHONE_NUMBER` in international format.
First login prompts for the code and any two-step password. The configured
`TELEGRAM_AUTH_CODE` and `TELEGRAM_AUTH_PASSWORD` variables can supply them instead.

TDLib requires `libtdjson`. The Nix shell sets `TELEGRAM_TDLIB_LIBRARY_PATH`;
outside Nix, set that variable or `tdlib_library_path` in the config.

```bash
recall capture telegram tdlib-once --root /path/to/archive
recall capture telegram tdlib-run --root /path/to/archive --max-updates 50
recall capture telegram tdlib-daemon --root /path/to/archive
recall state show telegram --root /path/to/archive
recall normalize telegram --root /path/to/archive --date 2026-03-31
recall entities sync telegram --root /path/to/archive --date 2026-03-31
```

Use one process per TDLib account state. Received updates enter a durable
`pending.sqlite3` beside the account's TDLib directories. Capture acknowledges
receipts after durable raw storage and cursor save. A retry checks saved records
rather than appending the same receipt again. Corrupt or conflicting records stop
capture without dropping pending evidence. Batch limits cap delivered updates,
not queue growth during lookups. Use a wall-clock limit for supervised tests.

Drain a copied queue without credentials, TDLib or live lookups:

```bash
recall capture telegram drain --root /path/to/copied-capture --max-updates 1000
```

Drain changes the selected queue, raw log and cursor. Preserve originals together.
Saved chat/user labels describe observations, not historical ownership. An empty
queue does not prove complete history. Use a date-range rebuild to place late
receipts on their message day.

Desktop import accepts extracted collection exports or ZIP bundles:

```bash
recall import telegram-export /path/to/export --root /path/to/archive
```

Supported JSON has `chats.list`. Single-chat layouts are rejected. Partial imports
report skipped messages while retaining the original JSON. Media must remain
inside the retained bundle; absolute paths, parent traversal and escaping
symlinks are rejected. Missing export media never triggers TDLib fallback.

Each export bundle has scoped event, artifact, identity, conversation and thread
keys. Equal names or numeric keys do not link exports to native Telegram people.
Use explicit reviewed resolutions for ownership. Namespace changes require fresh
isolated replay, not in-place migration of old events or acquired bytes.

### BlueBubbles

Copy [bluebubbles.toml.example](../config/sources/bluebubbles.toml.example) to
`config/sources/bluebubbles.toml`. The example uses a LAN bind and a placeholder
token. Set `webhook_bind_host = "127.0.0.1"` for loopback use, replace the token
with a private value, and adapt `server_url` to your server. A token is required
even on loopback. LAN access needs explicit network protection.

```bash
recall capture bluebubbles serve --root /path/to/archive --skip-recovery
recall state show bluebubbles --root /path/to/archive --json
```

Register `/bluebubbles/webhook?token=...` at the Recall host reachable from the
BlueBubbles server. `0.0.0.0` is a bind address, not the destination URL. Keep
token-bearing query strings out of reverse-proxy logs. Recall disables Uvicorn
access logging and redacts startup hints.

Webhooks return 400 for malformed input, 401 for failed authentication and 503 for
storage failure or writer contention. Raw writes are synced before the cursor
advances. Unknown structured event types remain raw evidence without advancing
the message cursor. Repeated deliveries may remain in raw logs.

For bounded outage recovery, configure `server_url` and export the password
variable named by `password_env_var`, normally `BLUEBUBBLES_PASSWORD`:

```bash
recall capture bluebubbles recover --root /path/to/archive
recall normalize bluebubbles --root /path/to/archive --date 2026-03-31
recall entities sync bluebubbles --root /path/to/archive --date 2026-03-31
```

`serve` attempts startup recovery unless `--skip-recovery` is set. Recovery
validates pages and advances its cursor only after all pages succeed. Partial
raw evidence survives retry. Recovery and webhook capture share a writer lock.
Stopping the receiver does not itself recover missed delivery, and startup
recovery is not a recurring pull. Inspect actual receipts, not only `/healthz`.

Export historical Messages data on the Mac with Recall installed and access to
its Messages database:

```bash
recall export bluebubbles-history /path/to/bundle \
  --from 2026-03-27 --to 2026-04-02 --include-attachment-bytes
```

Omit `--include-attachment-bytes` for metadata only. Transfer the bundle, then:

```bash
recall import bluebubbles-export /path/to/bundle --root /path/to/archive
```

The bundle contains `manifest.json` and `messages.jsonl` at its top level.

### Local email, calendar and Asana

Email needs a configured notmuch database; calendar needs configured khal/vdirs.
These commands query the existing local stores instead of remote capture:

```bash
recall normalize email --root /path/to/archive --date 2026-03-31
recall entities sync email --root /path/to/archive --date 2026-03-31
recall normalize calendar --root /path/to/archive --date 2026-03-31 --timezone America/Edmonton
recall import asana-export /path/to/asana.json --root /path/to/archive
recall normalize asana --root /path/to/archive --date 2026-03-31
recall entities sync asana --root /path/to/archive --date 2026-03-31
```

Asana supports JSON exports, not direct API capture. Calendar records show plans,
not attendance. Journals read normalized events, not live notmuch or khal queries.

## Replay and backfill

`rebuild` reads saved raw evidence and selected local-query sources into a
separate non-overlapping output root:

```bash
recall rebuild --root /path/to/archive --output-root /path/to/replay \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton \
  --source slack --source telegram --source bluebubbles --source asana \
  --source email --source calendar
```

Dates are inclusive local event days, including DST boundaries. Replay scans
saved capture dates so late arrivals reach their event day. Successful source/
account contributions replace that scope; missing, failed or unsupported inputs
preserve previous contributions. `--account` filters labels, not remote accounts.
Email and calendar use `default`. Without an account filter, all accounts of the
selected source are in scope.

Coverage and input-hash checkpoints live under `data/state/rebuild/`. Repeat the
same command to resume. Raw caches are hash-checked; local queries run again.
Malformed input fails the source instead of certifying partial output. Rebuilds
lock their output workspace. Atomic file replacement is not a range transaction
or permission for concurrent direct normalizers.

For repeated event IDs on one day, replay's last processed observation wins in
capture-date/physical-line order, not source edit-time order. Distinct-ID counts
do not imply fuzzy deduplication. Raw logs retain other observations; normalized
events retain one selected raw reference.

Slack alone has remote date-range backfill:

```bash
recall backfill slack --root /path/to/archive --output-root /path/to/slack-raw \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton --account work
recall rebuild --root /path/to/slack-raw --output-root /path/to/slack-replay \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton --source slack
```

Each day publishes a hash-checked bundle. Reruns reuse verified days; conflicting
or changed captures are refused. Choose a new output root for a fresh pull or
changed options. Failed days stop the run. Rate-limit retries are bounded.
Threads expand from parents returned in that day's history query, so replies to
older parents may be missing. Archived conversations require configuration or
`--include-archived`. Backfill does not move live cursors or publish an index.

Optional audit timelines need the same timezone as replay:

```bash
recall timeline build --root /path/to/replay \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton
```

These quoted Org evidence reports live in `data/derived/timelines/`. They are not
narrative journals or timesheets. Rendering refuses edited or unowned outputs.

## Search and recall

```bash
recall search index --root /path/to/archive --include-root /path/to/accepted-replay
recall search query "school" --root /path/to/archive --from 2026-03-27 --to 2026-04-02 --json
recall search person "Alex" --root /path/to/archive --org
recall search person --identity ident_email_alex_example_com --root /path/to/archive --json
recall search project "Client project" --root /path/to/archive --json
recall search status --root /path/to/archive --json
```

The FTS5 index covers normalized evidence, not journal prose or Org notes. Later
included roots override identical event IDs. Calendar occurrences keep observed
days across roots. Failed indexing preserves the old database.

Queries match all supplied words and default to newest timestamps. Use
`--relevance`, `--source`, date bounds or `--limit` to narrow results. General
queries also accept `--identity`. JSON includes events and physical citations;
Org output quotes snippets and links to evidence. Person lookup separates
identity candidates and unlinked mentions. Project lookup does not infer
membership, completion or work hours. Both report indexed source date bounds.

`search status` compares indexed normalized membership/hashes and identity labels.
It exits 0 for unchanged inputs and 1 for stale or unverifiable inputs. It does
not check raw-byte integrity, prove complete capture, or rebuild anything.

For moved archives, set `RECALL_RELOCATION_MAP` to a JSON list of absolute `old`
and `new` paths. The most specific mapping wins without rewriting evidence:

```bash
RECALL_RELOCATION_MAP=/path/to/moves.json recall search query "school" --root /path/to/archive --json
```

Use `resolved_normalized_path` and `resolved_raw_path` only when their respective
citation error fields are null. Normalized citations must match the complete
stored event at its physical line. Missing email files after Maildir flag renames
resolve only to a unique same-directory file with the same stable filename and
recorded Message-ID. That does not prove unchanged message bytes.

The bundled [recall-history skill](../skills/recall-history/SKILL.md) routes agent
questions to read-only lookups. From this checkout, install only if absent:

```bash
mkdir -p "$HOME/.agents/skills"
ln -s "$PWD/skills/recall-history" "$HOME/.agents/skills/recall-history"
```

Reload skills or start a new agent session. The wrapper uses this checkout and
its Nix environment, not an unrelated `RECALL_ROOT`. It permits only query,
person and project lookup, with no ingestion or index override. It is not a
security sandbox. Agents must disclose stale evidence and keep identities separate.

## Identities and attachments

Sync source identities before reviewing matches. Manual overrides belong in
`config/entity-resolution/manual.toml`; see the [example](../config/entity-resolution/manual.toml.example).
`entities match` changes resolutions and may merge people, so review its scope:

```bash
recall entities show unresolved --root /path/to/archive --suggested-only
recall entities show identities --root /path/to/archive
recall entities show resolutions --root /path/to/archive
recall entities match --root /path/to/archive
```

Reused addresses need event-time validity bounds. A name or handle alone does
not prove ownership. Changed resolutions require refreshed normalized enrichment
and an index rebuild before derived views reflect them.

Metadata capture is the default. Downloads apply only to source-native files,
not arbitrary links in messages. Inspect a dry run before acquisition:

```bash
recall artifacts show --root /path/to/archive --date 2026-03-31
recall artifacts download slack --root /path/to/archive --date 2026-03-31 --dry-run
recall artifacts verify --root /path/to/archive --source telegram --date 2026-04-02 --json
recall events overlaps --root /path/to/archive --source telegram --account personal
```

`artifacts download` supports Slack, BlueBubbles and Telegram. Telegram copies
available local files first; TDLib fallback needs proven native raw provenance.
Acquired cache bytes must match SHA-256 and any recorded size. Missing or invalid
bytes fail without automatic repair. Forced replacement stages new bytes and
preserves prior acquired bytes/metadata if acquisition fails.

`artifacts verify` is offline and read-only. It reports valid, invalid,
unverifiable and not-acquired records, without credentials or remote locators.
Integrity failures and missing checksums exit 1. No acquired bytes is not a valid
cache; an empty metadata file does not establish coverage. Verification does not
remove, fetch or fix anything.

`events overlaps` reports repeated Telegram/BlueBubbles raw keys, hashes and all
physical observations. Export keys remain scoped to their bundles. It does not
join records, infer native/export equivalence or migrate old IDs.

## Journals

`journal build` reads normalized evidence and calls Pi. The default route is
`codex-lb/gpt-6.1-sol:medium`; override it with `--model`. Tools, extensions,
skills, project context and session persistence are disabled for this request.
Only journal instructions and evidence enter the model request. Approve remote
transmission first.

```bash
recall journal build --root /path/to/replay \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton \
  --author "Your name" --output /path/to/preview
```

Entries use `OUTPUT/YYYY/MM/YYYY-MM-DD.org`. Unchanged days reuse checkpoints;
`--regenerate` requests new output. Edited entries are protected. Invalid drafts
fail closed and remain under `data/derived/journal-failures/`. Citation validation
checks evidence membership, not narrative truth.

To replay selected sources and generate previews in one dedicated workspace:

```bash
recall journal run --root /path/to/archive --workspace /path/to/run \
  --from 2026-03-27 --to 2026-04-02 --timezone America/Edmonton \
  --author "Your name" --source slack --source email --source calendar
```

This still calls a model. Only `--capture-slack` opts into remote capture; other
sources need saved evidence or local queries. Failed/unsupported replay stops
generation; missing sources stay visible; no-evidence days skip the model.
Repeat the same command to resume. Previews live under `WORKSPACE/preview`, with
packets/revisions in `WORKSPACE/replay`. Nothing publishes to the main archive,
index or journal, and no recurring job is installed.

### Prepare or inspect without a model

```bash
packet="$(recall journal prepare --root /path/to/replay --date 2026-03-31 \
  --author "Your name" --timezone America/Edmonton)"
recall journal inspect --packet "$packet" --require-current
recall journal inspect --revision /path/to/revision/journal.org
```

Packets contain `prompt.org`, `events.jsonl` and hash-checked `packet.json`.
`prepare` and `build` accept repeatable `--include-root` for accepted normalized
replays. Later roots win identical IDs; missing files do not erase prior evidence.
Input roots remain read-only. Coverage and physical event origins follow each root.

Inspection checks immutable hashes and current citations. `--require-current`
additionally rejects changed or unverifiable normalized/coverage input hashes.
An intact frozen packet can remain valid after input drift. Older packets without
coverage hashes cannot certify currentness. New single-root packets record those
hashes, including a null hash for absent coverage; later creation counts as drift.
Inspection neither rewrites old packets nor establishes capture completeness.

For an externally drafted body, save a cited revision without a model call:

```bash
recall journal save --root /path/to/replay --packet "$packet" \
  --draft /path/to/draft-body.org --model actual-model-id --options '{}'
```

Bodies use `[fn:EVENT_ID]` markers and optional `**` sections, without titles,
links, footnote definitions or executable Org directives. Record only actual
model/options used. Each distinct saved revision has its own immutable directory.

### Review and publish

Publish the printed immutable revision after checking its claims and omissions:

```bash
recall journal publish --root /path/to/replay \
  --revision /path/to/revision/journal.org --output "$HOME/org/journal"
```

To correct prose, copy `body.org` to a separate file, retain its event citation
markers, and add `--draft /path/to/reviewed-body.org`. Do not edit immutable
revisions or rendered numbered footnotes. Publication saves a reviewed revision,
checks hashes/citations and Org safety, and refuses edited or unowned entries.
It does not call a model. Publication uses frozen evidence even if live inputs
changed; inspect currentness separately when it matters.

## Services and retention

[Systemd examples](../examples/systemd/) are host-specific starting points, not
portable installers. Check paths, package version, config, environment, backup
mounts and sandbox write access before enabling them. The BlueBubbles examples
use a pinned `package` symlink and an SSH tunnel to host `bub`; they disable
startup recovery and do not normalize, index or generate journals. Expired SSH
agent keys prevent reconnection.

With `ProtectHome=read-only`, allow writes to the actual capture path and its
persisted/bind-mounted path. A storage-only allowlist can leave the home path
read-only inside the service. On an impermanent root, persist or declaratively
manage units, capture paths and package retention. Check authenticated delivery
again after reboot; active units and `/healthz` are not capture proof.

Back up operational raw records and cursors together. Telegram also needs its
TDLib account state and pending queue. Retain acquired blobs, manual resolutions,
accepted replay roots, packets and journal revisions. Test restoration into a
private workspace before relying on it. Git exclusion and a storage-backed path
alone prove neither backup inclusion nor restoration.
