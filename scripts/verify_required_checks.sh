#!/bin/sh
set -eu

# This gives CI a safe, reproducible negative-control check. Setting the variable
# in a throwaway PR must make the required job fail before a merge can proceed.
if [ "${TEVARTA_CI_FORCE_FAILURE:-0}" = "1" ]; then
  echo "Intentional CI failure requested by TEVARTA_CI_FORCE_FAILURE."
  exit 1
fi

echo "Required-check guard passed."
