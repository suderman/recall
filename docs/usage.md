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

## Review voice candidates

The voice candidate command is an offline, read-only review. By default it reads
saved normalized email events and their original MIME messages, not an index or
live inbox:

```bash
recall voice candidates --root /path/to/archive \
  --identity ident_email_alex_example_org --account work \
  --from 2026-09-28 --to 2026-10-04 --limit 20
```

Choose sender identity IDs explicitly; repeat `--identity` for another identity.
Names, recipients, participants and person resolutions do not select authors.
Both date bounds are required. `--limit` bounds displayed records, including
exclusions; `truncated` and `missing_days` describe report limits, not coverage.
Output is JSON and may contain private writing. Keep saved reports owner-only.
The command creates no folders, updates no SQLite state and calls no model.

### Email

A reviewable email passage requires matching raw Message-ID, sender identity and
plain body text. Reports include the normalized physical line/file hash, resolved raw
path/hash, decoded MIME-part index, body hash and passage offsets/hash. Offsets
are zero-based Unicode characters in that decoded part, with an exclusive end.
Existing relocation maps and unique Maildir flag renames remain supported.

Only a leading passage is proposed. Recognized quoted/forwarded tails and
signature blocks are omitted, with offset ranges and reasons rather than their
contents. Extractor version 2 also omits isolated final paragraphs matching the
raw sender's display name or its first word as possible sign-offs, without removing
names inside ordinary prose. Indented closings are supported. Unrecognized
sender-attribution lines and image placeholders in the proposed prefix reject
the whole candidate rather than guessing an original/quoted boundary. Earlier
reports keep their recorded extractor version and are never upgraded in place.

Inline answers after a quote are not recovered. Unknown reply/forward
boundaries, quotation marks inside the proposed passage, HTML-only or multiple
plain bodies, malformed/mismatched raw evidence and unsupported sources are
excluded. Without an explicit `--source telegram`, chat records remain unsupported.
Journal records are always unsupported.

### Native Telegram

Telegram review requires an explicit source, account, sender identity and both
date bounds. These placeholder values are not identity or authorship approvals:

```bash
recall voice candidates --root /path/to/archive --source telegram \
  --account personal --identity ident_telegram_user_123 \
  --from 2026-09-28 --to 2026-10-04 --limit 20
```

Saved native `updateNewMessage` envelopes from `tdlib-once`, `tdlib-run`,
`tdlib-daemon` or `pending-offline` captures are supported. Saved `tdlib-history`
envelopes have a separate proof rule described below. The selected raw
physical line must match the account, message/chat IDs, recomputed event ID,
user sender ID, timestamp, conversation and exact normalized text. The raw
payload must include exactly one matching sender user with `userTypeRegular`.
Missing user metadata does not establish that the sender is human. Outgoing
flags, participants, display names and channel ownership cannot select self.

Extractor version 1 proposes the whole verified `messageText` body, without
trimming or splitting it. Passage offsets are zero-based Unicode characters in
that raw body, with an exclusive end. Reports include resolved raw path/file hash,
physical line/record hash, sender ID and body/passage hashes. Raw and normalized
inputs must remain unchanged during inspection. Existing relocation maps apply.

Desktop imports, file-backed/manual capture modes, channel senders, forwards,
bot users or bot-origin messages, service content and media captions are excluded.
The body must have an empty formatted-entity list. Quote/code entities and all
other formatting are excluded rather than converting TDLib's UTF-16 offsets.
Literal quotation, forwarding and code markers also exclude the whole body.
An ordinary reply may propose its own plain body; reply linkage alone does not
make it quoted history. Links are text and are never fetched.

#### Saved native history proof

A `tdlib-history` envelope must honestly identify its `getChatHistoryMessage`
shape. It is not relabelled as a delivered `updateNewMessage`. Native-history
proof version 1 requires saved authenticated-self, private-chat/page and capture
receipts alongside the raw message. The receipts must be owner-only regular files.
The collector reads them through the existing relocation map and records their
physical paths and hashes. Plaintext extractor version 1 is unchanged.

The proof binds the exact native authenticated user, regular private peer, account,
frozen capture cutoff, raw file hash and selected cohort count. It checks bounded
`getChatHistory` queries, advancing page cursors, descending message IDs/dates,
the recorded own-message cap or reached date boundary, and the message's exact
page/index/body hash. Missing, ambiguous, malformed, changing or public proof is
not an empty result and prevents collection from replacing a saved scope.

These hashes bind saved local observations. They are not server signatures or
proof of complete Telegram history. Unselected page bodies are not retained and
cannot be reconstructed from page hashes. Authentication proves delivery identity,
not human composition. A separate original-writing grant is still required.
Formatting, quote, forward, bot and media exclusions apply unchanged. Exact-span
grants do not transfer to body edits. Collection and inspection recheck receipt
bytes and private permissions before accepting current output.

This is a saved-input reader, not a new history-fetch CLI. It does not initialize
TDLib, fetch another page, download media or extend a cohort's authorship grant.

### Review limits

Automated/bulk mail and records tagged `generated`, `ai_generated` or
`ai_assisted` are excluded. Use repeatable `--exclude-event EVENT_ID` for known
model-written or otherwise unsuitable records. Absence of a tag does not prove
human authorship, and Recall does not detect AI assistance automatically.

Every result is either `needs_review` or `excluded`, always with
`corpus_eligible: false`. Candidate selection is not authorship confirmation.
Only human-confirmed original writing may enter a corpus. This command does not
build a profile, rewrite text, change journal prompts or admit samples.

## Collect local voice samples

Collection reads saved normalized and raw evidence only. It does not capture
messages, query accounts, change SQLite, run a model or change journal prompts.
It supports the email and native Telegram extractors described above. Unlike
candidate review, it can admit text under an explicit original-writing policy.
Account ownership and a matching sender alone do not prove human composition.

Create an owner-only JSON policy, then select one day, source and account:

```bash
chmod 600 /path/to/private-policy.json
recall voice collect --root /path/to/archive --date YYYY-MM-DD \
  --source telegram --account ACCOUNT --policy /path/to/private-policy.json
recall voice inspect --root /path/to/archive --date YYYY-MM-DD --require-current
```

These commands are syntax examples, not approval of any identity or origin window.
There is no default identity, grant or policy path. The policy must be a regular
file owned by the current user, with no group or other permissions. Symlink
policies are rejected. This empty policy admits nothing:

```json
{
  "format": "recall-voice-policy-v1",
  "ownerships": [],
  "origin_grants": [],
  "denials": [],
  "exclude_events": []
}
```

All fields are required except the optional grant `span`. Unknown fields and
duplicate JSON keys are rejected.

Each `ownerships` object requires:

- `id`: a unique policy ownership ID.
- `source`: `email` or `telegram`.
- `account`: the exact normalized source account.
- `identity`: the exact sender identity ID, not a person ID or display name.
- `from` and `to`: UTC timestamps with `Z` or `+00:00`, both inclusive. Date-only
  values and reversed bounds are rejected.
- `conversations`: `null` for all conversations in that scope, or a nonempty list
  of exact normalized conversation IDs.

Each `origin_grants` object requires `id`, `ownership_id`, `from`, `to`,
`conversations` and `assertion`. Grant IDs must be unique. `ownership_id` must name
an ownership object. Grant time and conversation bounds cannot exceed that
ownership. The literal assertion `original-human-unassisted` records the user's
confirmation that qualifying prose in this context and time window is original
writing, without AI drafting, AI rewriting or copied third-party text, except
excluded events. Recall does not independently verify that assertion. Do not add
it when composition is uncertain.

An optional `span` restricts a grant to one exact extractor proposal. Its required
fields are `event_id`, `body_sha256`, `start`, `end`, `sha256` and
`extractor_version`. Offsets are zero-based Unicode characters with an exclusive
end. The hashes, offsets and version must match the current proposal exactly.
This is not a custom excerpt selector. A receipt for an old body does not approve
an edit, even when the event ID stays the same.

Each `denials` object has the same fields as an ownership object, with a unique
ID within the denial list. It denies composition approval within that scope.
`exclude_events` is a list of event IDs that must stay out. Denials, exclusions,
generated/assisted tags and extractor exclusions override grants. Multiple
matching ownerships or origin grants exclude the affected event rather than
choosing one. A clean candidate without a matching grant remains `needs_review`.

Collection writes `data/derived/voice/YYYY/YYYY-MM-DD.json`. Decisions and eligible
spans share one atomic day file. Voice directories are mode `0700`; files are
mode `0600`. Existing public or symlink outputs are rejected, not repaired by
changing their permissions. The file retains other source/account scopes when
one scope succeeds. Exact retries leave its bytes and modification time unchanged.
Successful edits or policy changes replace that scope's eligible set. Span IDs
bind source, account, event, body hash, offsets and extractor version. Appending
unrelated raw evidence changes snapshot hashes, not the span's identity.

Eligible records retain private prose and physical provenance, including normalized
line/record/file hashes, raw references and selected raw hashes, body/span hashes,
and ownership/grant hashes. Email records also retain decoded MIME-part context.
Review and excluded records do not retain passage text. Treat the entire file as
private even when every passage is omitted. CLI summaries and freshness inspection
do not print sample text.

The collector checks the full selected saved day scope without a display limit.
Missing, incomplete, malformed, changing or unverifiable inputs leave the prior
day result unchanged and fail the command. An empty or absent source/account
contribution is not a deletion receipt and also fails. Removing events from an
existing scope fails even when the shorter input is valid JSONL. A complete-line
truncation cannot establish that those events were deleted. A successful scoped
replay receipt can authorize removal or an empty contribution. Policy changes can
revoke admission while the event records remain. Collection records and checks
the supplied local snapshot, not remote capture coverage.

Saved results are snapshots, not permanent freshness claims. Inspect before using
samples. Inspection re-evaluates every retained scope against current policy and
evidence and detects changed admissions as well as changed hashes. `--require-current`
returns a nonzero exit status if any scope cannot be verified current. The
collector's successful summary verifies only the selected scope; retained scopes
may need their own reconciliation. Failed collection does not make old eligible
records safe to use. Keep one writer per workspace; concurrent collection is
rejected by the voice writer lock. Profiles and voice-based rewrites are not
implemented.

### Replay receipts and opt-in collection

A replay manifest records successful publication of a selected contribution.
Supply it with `--coverage` when collecting from that replay output:

```bash
recall voice collect --root /path/to/replay-output --date YYYY-MM-DD \
  --source telegram --account ACCOUNT --policy /path/to/private-policy.json \
  --coverage /path/to/replay-output/data/state/rebuild/manifest.jsonl
```

The receipt must come from this output workspace's existing replay manifest.
Its job must select the exact source, explicit account and day and have status
`success`, `captured-empty` for Telegram or `queried-empty` for email. The saved
contribution snapshot and count must match the current normalized scope exactly.
Telegram receipts also require unchanged recorded raw input hashes and, for an
empty contribution, saved capture-day evidence. Missing, failed or unsupported
jobs cannot authorize deletion. Manifest, contribution and input snapshot files
must be owner-only regular files. The collector checks receipts and saved inputs
again before publication. It never creates or repairs a receipt.

An empty receipt replaces that scope's records with an empty list, retaining
other scopes. A receipt for a shorter successfully published contribution can
remove old events. A shortened normalized file alone still fails. Inspection
rechecks the receipt and its snapshots; later changes make the result unverified.
An unrelated manifest job does not invalidate a still-matching scope receipt.
Receipts describe saved local replay publication, not complete remote history or
a fresh query of the current Maildir. New capture or later authoritative-store
changes still need normalization or replay.

Collection is off by default. These commands opt in after successful publication:

```bash
recall normalize email --root /path/to/archive --date YYYY-MM-DD \
  --account ACCOUNT --voice-policy /path/to/private-policy.json
recall normalize telegram --root /path/to/archive --date YYYY-MM-DD \
  --voice-account ACCOUNT --voice-policy /path/to/private-policy.json
recall rebuild --root /path/to/archive --output-root /path/to/replay-output \
  --from YYYY-MM-DD --to YYYY-MM-DD --source telegram --account ACCOUNT \
  --voice-policy /path/to/private-policy.json
```

Email normalization requires an explicit `--account` when opting in. Telegram's
`--voice-account` selects collection only; normalization still publishes all
captured accounts as before. Replay opt-in requires an explicit `--account` and
only email or Telegram sources. Replay queries email through its existing local
notmuch path; voice collection itself never queries an account.

Normalizers merge events and provide no deletion receipt. Their hooks cannot
remove absent events or admit an empty scope. Replay publishes scoped replacement
and passes its checked receipt to the same collector. Hooks run only after durable
normalized output, and for replay after the manifest is written. They never run
on raw capture, cursor updates, staging output or unsuccessful replay jobs.

An opted-in command prints a separate `voice=` JSON summary without sample prose.
If collection fails, the command exits nonzero while already published normalized
evidence and replay status remain intact. The previous voice result is retained
and must not be used without current inspection. Voice summaries are not written
into replay manifests. Without these options, publication and CLI output are
unchanged. No timer, capture listener, model call or profile build is added.

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

#### Preview a bounded model input

`journal input-view` previews a separate versioned JSON view. By default it prints
only counts, hashes and byte size, not source prose. It checks the frozen packet's
hashes, not current source freshness. Run strict inspection separately:

```bash
recall journal inspect --packet "$packet" --require-current
recall journal input-view --packet "$packet"
recall journal input-view --packet "$packet" \
  --event evt_example --event evt_other --output /path/to/private/view.json
```

The default byte budget is 131,072 bytes for the encoded view. `--max-bytes` changes
that explicit local preview budget; it is not a token estimate or model context
limit. Over-budget views fail before export. No text is truncated. A large full
day may still exceed the limit after removing transport fields.

The view retains full selected event text, quoted history, attribution, tags,
other normalized fields and frozen coverage warnings. It omits `raw_ref` and the
standalone `source_urls` list; URLs already present in text are not rewritten.
Full records and physical provenance remain in the original packet. Packet,
evidence and original prompt hashes bind the view to that snapshot. Citation
markers refer only to selected event IDs. Original packet instructions are not
modified; the view carries the current versioned journal template separately.

Without `--event`, every event is selected. Repeating `--event` makes selection
explicit and lists every omitted event ID. Unknown or duplicate IDs fail. No
marketing classifier silently removes events, and omitted content must not be
inferred. Review selection for factual gaps before any later generation.

Exports require an owner-only directory and create mode `0600` files. Public or
symlink files are rejected. Export inside packet/evidence data directories is
refused. A writer lock and atomic replacement protect the output. Exact retries
leave bytes and mtime unchanged; edited files are never overwritten.

Preview/export does not call a model or change accepted packets or revisions.
The opt-in generation route below is separate. Approve the selected evidence,
model route and transmission before running it.

#### Generate from one selected view

`journal build --input-view` opts in to one explicit exported view for one day.
It does not choose events, trim quoted history or split a day into model calls.
After approving transmission, use the same root, ordered include-roots, date,
author and timezone that produced the view's packet:

```bash
recall journal build --root /path/to/replay \
  --from 2026-03-31 --to 2026-03-31 --author "Your name" \
  --timezone America/Edmonton --output /path/to/private/previews \
  --input-view /path/to/private/view.json --max-input-bytes 131072
```

The view must be an owner-only regular file in canonical exported form. Its
packet, selected events, complete text, attribution, coverage, citation markers
and current instructions must match. Changed, stale, public or symlink views
fail before runner startup. This mode also requires unchanged normalized/coverage
inputs and resolved citations. Use the documented relocation map when needed.

The default generation budget is 131,072 bytes for the sum of the UTF-8 user and
system prompts, including the selected-input wrapper. `--max-input-bytes` changes
that explicit budget. It is not a token limit, price estimate or measurement of
provider framing. A view that fits preview's `--max-bytes` may still exceed the
assembled generation budget. Failure leaves the previous published journal intact.

Each selected revision keeps its exact private `model-input.json`. Generation
metadata binds the view hash, selected IDs and original request budget. Cache
identity binds the view and selection, separately from the legacy route. The
current byte limit is checked before cache use. Changing only a passing limit
can reuse existing content without another model call; the revision keeps the
original call's budget. Identical view bytes at a different path can also reuse
the same cache. Old instructions remain
frozen with saved revisions; they are not upgraded during inspection or publication.
The generated Evidence section states selected and omitted counts. The renderer
rejects citations to omitted events, even when those events exist in the full
packet. Reviewing a body through `journal publish --draft` preserves that rule
and the original view. Rejected drafts retain their view alongside failure metadata.

Without `--input-view` or SDK mode below, the legacy full-packet route remains
unchanged and uncapped by this option. `--max-input-bytes` without either mode is
rejected rather than implying protection. `journal run` does not select these
views or use SDK mode. Tests use synthetic packets and fake runners; a real
model request still needs approval.

#### Explicit single-request SDK mode

The default CLI route invokes `pi` through PATH and inherits its settings,
including retries, compaction and eligible cache warming. An input byte limit
does not control those extra model requests or the total bill.

Opt in to the packaged SDK bridge by providing both trusted local paths:

```bash
recall journal build --root /path/to/replay \
  --from 2026-03-31 --to 2026-03-31 --author "Your name" \
  --timezone America/Edmonton --output /path/to/private/previews \
  --input-view /path/to/private/view.json --max-input-bytes 131072 \
  --node-executable /absolute/path/to/node \
  --pi-sdk /absolute/path/to/pi-coding-agent/dist/index.js
```

Node and the Pi SDK are external runtime prerequisites for this optional mode.
Recall does not install them, inspect launcher scripts or guess SDK locations.
Missing/unsupported configuration fails; it never falls back to the inherited CLI
route. Paths identify executable code and must come from the operator, not source
evidence. SDK mode does not invoke the user's `pi` shell launcher or its dotenv/
service hooks. Required provider environment variables must already be available.

The bridge applies in-memory settings with zero agent/provider retries, no
compaction or cache warming, no tools/resources and no persisted session. It
requires a configured physical chat model, not virtual model routing. Original
core SDK auth/model storage is used under `PI_CODING_AGENT_DIR` or its standard
default. No credential files are copied or restored. Normal SDK OAuth refresh
uses its original store; normal runtime model-catalog caching remains SDK-owned.
User settings and model configuration are not rewritten.

A stream guard rejects a second call before provider entry. It rejects changed
user messages or tool context and supplies only the approved user/system text,
not SDK-added working-directory/date system context. Unexpected auxiliary events,
ambiguous completion, malformed protocol and failed responses prevent publication.
Each uncached generated day gets one guarded stream; an explicitly requested
multi-day build may generate one per day. This does not bound authentication
requests, provider framing or opaque retries inside a remote proxy.

SDK mode defaults to a 131,072-byte user-plus-system prompt budget even without
`--input-view`. The limit is checked before process startup and cache use.
Node path, SDK entry digest, bridge digest and fixed policy bind cache/provenance,
separately from inherited CLI records. Changing only a passing byte limit can
reuse content; changing runner identity/policy cannot. Old immutable revisions
remain readable without upgrading their policy. SDK mode clears inherited Node
preloads and parent Pi session markers. No live request is authorized by installing
or configuring the bridge.

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
