#!/usr/bin/env bash
# Nightly Recall loop: capture, replay, index, then draft yesterday's journal.
#
# Layout under RECALL_HOME (default $XDG_DATA_HOME/recall):
#   archive/          main archive: raw captures, identities, source config
#   bluebubbles/      optional BlueBubbles receiver root (capture spool)
#   replays/<name>/   rebuilt normalized days; every one is indexed
#
# Journal generation sends the selected evidence to the configured model. The
# runner needs that provider's environment, for example
# EnvironmentFile=%h/.pi/agent/.env, plus RECALL_NODE_EXECUTABLE and RECALL_PI_SDK.
set -euo pipefail

home=${RECALL_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/recall}
archive=$home/archive
tz=${RECALL_TIMEZONE:-America/Edmonton}
author=${RECALL_AUTHOR:?Set RECALL_AUTHOR to the journal author name}
# The real journal. Entries are written once; edited entries are never overwritten.
output=${RECALL_JOURNAL_OUTPUT:-$HOME/org/journal}
day=${1:-$(TZ=$tz date -d yesterday +%F)}
today=$(TZ=$tz date +%F)

# Continue from saved evidence after a capture failure, but fail the service
# after publication so its failure alert still reaches the author.
capture_status=0
if [[ -f $archive/config/sources/slack.toml ]]; then
  if ! recall capture slack --root "$archive" --incremental; then
    capture_status=1
    echo "recall-nightly: Slack capture failed; continuing with saved evidence" >&2
  fi
fi

# Replay through today so late arrivals for the journal day are included.
recall rebuild --root "$archive" --output-root "$home/replays/local" \
  --from "$day" --to "$today" --timezone "$tz" \
  --source email --source calendar --source slack
journal_roots=()
if [[ -d $home/bluebubbles/data ]]; then
  recall rebuild --root "$home/bluebubbles" --output-root "$home/replays/bluebubbles" \
    --from "$day" --to "$today" --timezone "$tz" --source bluebubbles
  journal_roots+=(--include-root "$home/replays/bluebubbles")
fi

# Later include roots win identical event IDs; order by name for stability.
index_roots=()
for replay in "$home"/replays/*/; do
  index_roots+=(--include-root "${replay%/}")
done
recall search index --root "$archive" "${index_roots[@]}"

recall journal build --root "$home/replays/local" "${journal_roots[@]}" \
  --people-root "$archive" --from "$day" --to "$day" --timezone "$tz" \
  --author "$author" --select default --output "$output"

exit "$capture_status"
