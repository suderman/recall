#!/usr/bin/env bash
set -euo pipefail

case "${1-}" in
  query|person|project) ;;
  *) printf "%s\n" "Usage: lookup.sh query|person|project [search arguments]" >&2; exit 2 ;;
esac
for argument in "$@"; do
  case "$argument" in
    --root|--root=*|--index|--index=*|--)
      printf "%s\n" "This wrapper uses the main archive index; root/index overrides are not allowed." >&2
      exit 2 ;;
  esac
done

script=$(realpath -- "${BASH_SOURCE[0]}")
checkout=$(realpath -- "$(dirname -- "$script")/../../..")
# The main archive lives outside the checkout; older setups kept it inside.
archive="$HOME/.local/share/recall/archive"
[ -d "$archive/data" ] || archive=$checkout
exec nix develop --offline "$checkout" -c recall search "$@" --root "$archive"
