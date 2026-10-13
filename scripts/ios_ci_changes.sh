#!/bin/sh
set -eu

# Prints `ios=true` when the iOS CI build must run, else `ios=false`.
# Usage: ios_ci_changes.sh <event> <base-sha> <head-sha>
# An unknown range (manual run, new branch, missing commit) builds rather than skips.
event="$1" base="${2:-}" head="${3:-}"

if [ "$event" = "workflow_dispatch" ] || [ -z "$base" ] || [ -z "$head" ]; then
  echo "ios=true"
  exit 0
fi
if ! changed="$(git diff --name-only "$base" "$head" 2>/dev/null)"; then
  echo "ios=true"
  exit 0
fi

# Inputs the Swift package and app build read: the app, the shared v1 fixtures it
# decodes, the v1 contract, the make targets, and this workflow.
if printf '%s\n' "$changed" | grep -qE \
  '^(ios/|fixtures/v1/|contracts/v1/|Makefile$|\.github/workflows/ios\.yml$|scripts/ios_ci_changes\.sh$)'; then
  echo "ios=true"
else
  echo "ios=false"
fi
