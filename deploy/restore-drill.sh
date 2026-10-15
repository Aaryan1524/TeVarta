#!/usr/bin/env bash
# Prove a Postgres backup restores: decrypt, restore into a throwaway database, and
# compare row counts with production. Never touches the live database. (MVP §20.1: an
# untested backup is not a backup.) Usage: restore-drill.sh BACKUP.dump.age [AGE_IDENTITY_FILE]
set -euo pipefail
FILE="${1:?backup file}"; IDENTITY="${2:-}"
COMPOSE_FILE="${COMPOSE_FILE:-$(dirname "$0")/compose.production.yaml}"
compose() { docker compose -f "$COMPOSE_FILE" "$@"; }
DRILL="restore_drill_$(date -u +%s)"

decrypt() {
  if [ -n "$IDENTITY" ]; then age -d -i "$IDENTITY" "$FILE"
  elif [ "${DRILL_PLAINTEXT:-0}" = 1 ]; then cat "$FILE"
  else echo "pass the age identity file as the second argument" >&2; return 1; fi
}

compose exec -T postgres createdb -U tevarta "$DRILL"
trap 'compose exec -T postgres dropdb -U tevarta --if-exists "$DRILL"' EXIT
decrypt | compose exec -T postgres pg_restore -U tevarta -d "$DRILL" --no-owner --exit-on-error --single-transaction

count() { compose exec -T postgres psql -U tevarta -d "$1" -tAc \
  "select 'entities='||(select count(*) from entities)||' articles='||(select count(*) from articles)||' aliases='||(select count(*) from entity_aliases)"; }
echo "restored: $(count "$DRILL")"
echo "live:     $(count tevarta)"
echo "drill database dropped. Compare the two lines; small drift is the time since the backup."
