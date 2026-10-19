#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
TEST_TIMEOUT_SECONDS="${TEST_TIMEOUT_SECONDS:-20}"
TMP_ROOT="$(mktemp -d /tmp/manubis-tests.XXXXXX)"
trap 'rm -rf "$TMP_ROOT"' EXIT HUP INT TERM
umask 077
mkdir -p "$TMP_ROOT/home" "$TMP_ROOT/tmp" "$TMP_ROOT/cache"

# Deliberately discard inherited application credentials and network settings.
env -i \
  PATH="$PATH" \
  HOME="$TMP_ROOT/home" \
  TMPDIR="$TMP_ROOT/tmp" \
  XDG_CACHE_HOME="$TMP_ROOT/cache" \
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONHASHSEED=0 \
  PYTHONPATH="$ROOT/src:$ROOT/scripts/test_support" \
  TEST_TIMEOUT_SECONDS="$TEST_TIMEOUT_SECONDS" \
  "$PYTHON" "$ROOT/scripts/test_support/unittest_runner.py" "$ROOT/tests"
