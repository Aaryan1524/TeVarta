#!/usr/bin/env bash
# Nightly backup: Postgres (pg_dump) and Neo4j (offline dump), encrypted with age, then
# uploaded off-host with rclone. Run from cron or the systemd timer (see README).
# Neo4j Community can only dump while stopped, so it is down for about a minute.
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-$(dirname "$0")/compose.production.yaml}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/tevarta}"
KEEP_LOCAL_DAYS="${KEEP_LOCAL_DAYS:-7}"
PROJECT="${COMPOSE_PROJECT_NAME:-tevarta}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
umask 077
mkdir -p "$BACKUP_DIR"

compose() { docker compose -f "$COMPOSE_FILE" "$@"; }
PARTIAL=()
cleanup_partials() { for f in "${PARTIAL[@]:-}"; do [ -n "$f" ] && rm -f "$f"; done; }
trap cleanup_partials EXIT

# A file only gets its final name after the whole pipeline succeeded, so a failed
# backup can never leave an empty or truncated "backup" that looks real.
publish() { mv "$1.partial" "$1"; }

encrypt() {  # stdin -> stdout. A backup that cannot be encrypted is not written.
  if [ -n "${BACKUP_AGE_RECIPIENT:-}" ]; then age -r "$BACKUP_AGE_RECIPIENT"
  elif [ "${BACKUP_ALLOW_PLAINTEXT:-0}" = 1 ]; then cat
  else echo "set BACKUP_AGE_RECIPIENT (age public key)" >&2; return 1; fi
}

echo "postgres: dumping"
PG_OUT="$BACKUP_DIR/postgres-$STAMP.dump.age"; PARTIAL+=("$PG_OUT.partial")
compose exec -T postgres pg_dump -U tevarta -Fc --no-owner tevarta \
  | encrypt > "$PG_OUT.partial"
publish "$PG_OUT"

echo "neo4j: stopping for an offline dump"
NEO_OUT="$BACKUP_DIR/neo4j-$STAMP.tar.age"; PARTIAL+=("$NEO_OUT.partial")
compose stop neo4j >/dev/null
trap 'compose start neo4j >/dev/null; cleanup_partials' EXIT   # never leave Neo4j down
docker run --rm -v "${PROJECT}_neo4j-data:/data" \
  --entrypoint sh neo4j:5.26-community -c \
  'mkdir -p /tmp/dump && { neo4j-admin database dump neo4j --to-path=/tmp/dump >/tmp/dump.log 2>&1 || { cat /tmp/dump.log >&2; exit 1; }; } && tar -C /tmp -cf - dump' \
  | encrypt > "$NEO_OUT.partial"
publish "$NEO_OUT"
compose start neo4j >/dev/null
trap cleanup_partials EXIT

if [ -n "${BACKUP_REMOTE:-}" ]; then
  echo "uploading to $BACKUP_REMOTE"
  rclone copy "$BACKUP_DIR" "$BACKUP_REMOTE" --include "*-$STAMP.*" --checksum
  date -u +%s > "$BACKUP_DIR/last-success"
else
  echo "BACKUP_REMOTE is not set: backups stay on this host only" >&2
fi

find "$BACKUP_DIR" -name '*.age' -mtime "+$KEEP_LOCAL_DAYS" -delete
echo "done: $STAMP"
