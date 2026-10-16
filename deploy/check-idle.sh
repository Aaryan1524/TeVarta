#!/usr/bin/env bash
# Oracle may reclaim an Always Free instance whose CPU, network AND memory all stay under
# 20% (95th percentile over 7 days). Memory is the lever we control: the databases hold
# data in RAM, so used memory should stay well above 20%. Warn below 25%.
set -euo pipefail
read -r total used < <(free -m | awk '/^Mem:/ {print $2, $3}')
pct=$(( used * 100 / total ))
echo "memory in use: ${pct}% (${used} of ${total} MB)"
if [ "$pct" -lt 25 ]; then
  echo "WARNING: memory is close to Oracle's idle-reclaim line (20%). Raise Neo4j page cache" >&2
  echo "or Postgres shared_buffers, or upgrade the account to pay-as-you-go." >&2
  exit 1
fi
