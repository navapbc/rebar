#!/usr/bin/env bash
# Fixture: bash test file that uses mktemp WITH trap EXIT cleanup
# Expected: passes no-temp-without-cleanup rule

TMPDIR=$(mktemp -d)
trap 'rm -rf "$TMPDIR"' EXIT

echo "doing work in $TMPDIR"
