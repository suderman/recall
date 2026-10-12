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
output=${RECALL_JOURNAL_OUTPUT:-$home/journal-preview}
day=${1:-$(TZ=$tz date -d yesterday +%F)}
today=$(TZ=$tz date +%F)

# Remote capture first; replay reads only saved evidence. A failed capture
# leaves a visible coverage gap in the journal rather than skipping the night.
if [[ -f $archive/config/sources/slack.toml ]]; then
  recall capture slack --root "$archive" --incremental ||
    echo "recall-nightly: Slack capture failed; continuing with saved evidence" >&2
fi

# Author notes (config/sources/notes.toml) correct and extend drafts. Redraft any
# of the previous week's days whose notes changed after its draft was written.
changed=()
sources=(--source email --source calendar --source slack)
notes_config=$archive/config/sources/notes.toml
if [[ -f $notes_config ]]; then
  sources+=(--source notes)
  notes_dir=$(sed -n 's/^directory *= *"\(.*\)"$/\1/p' "$notes_config")
  notes_dir=${notes_dir/#\~/$HOME}
  for back in 7 6 5 4 3 2 1; do
    earlier=$(TZ=$tz date -d "$day -$back days" +%F)
    note=$notes_dir/${earlier:0:4}/${earlier:5:2}/$earlier.org
    draft=$output/${earlier:0:4}/${earlier:5:2}/$earlier.org
    if [[ -f $note && ( ! -f $draft || $note -nt $draft ) ]]; then
      changed+=("$earlier")
    fi
  done
fi
days=("${changed[@]}" "$day")
first=${days[0]}

# Replay through today so late arrivals for the journal days are included.
recall rebuild --root "$archive" --output-root "$home/replays/local" \
  --from "$first" --to "$today" --timezone "$tz" "${sources[@]}"
journal_roots=()
if [[ -d $home/bluebubbles/data ]]; then
  recall rebuild --root "$home/bluebubbles" --output-root "$home/replays/bluebubbles" \
    --from "$first" --to "$today" --timezone "$tz" --source bluebubbles
  journal_roots+=(--include-root "$home/replays/bluebubbles")
fi

# Later include roots win identical event IDs; order by name for stability.
index_roots=()
for replay in "$home"/replays/*/; do
  index_roots+=(--include-root "${replay%/}")
done
recall search index --root "$archive" "${index_roots[@]}"

# One day at a time: only days with new evidence or notes regenerate.
for target in "${days[@]}"; do
  recall journal build --root "$home/replays/local" "${journal_roots[@]}" \
    --people-root "$archive" --from "$target" --to "$target" --timezone "$tz" \
    --author "$author" --select default --output "$output"
done
