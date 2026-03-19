#!/bin/sh
set -eu

SESSION_ROOT="${NOOB_SESSION_ROOT:-/mnt/session}"

mkdir -p \
  "${SESSION_ROOT}/control" \
  "${SESSION_ROOT}/events" \
  "${SESSION_ROOT}/state" \
  "${SESSION_ROOT}/artifacts"

exec node /app/noob_runner.mjs
