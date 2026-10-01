#!/usr/bin/env bash
set -euo pipefail

case "${1-}" in
  query|person|project) ;;
  *) printf "%s\n" "Usage: lookup.sh query|person|project [search arguments]" >&2; exit 2 ;;
esac
for argument in "$@"; do
  case "$argument" in
    --root|--root=*|--index|--index=*|--)
      printf "%s\n" "This wrapper uses its checkout index; root/index overrides are not allowed." >&2
      exit 2 ;;
  esac
done

script=$(realpath -- "${BASH_SOURCE[0]}")
root=$(realpath -- "$(dirname -- "$script")/../../..")
exec nix develop --offline "$root" -c recall search "$@" --root "$root"
