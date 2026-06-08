#!/usr/bin/env bash
# Fixture: bash test file that reads from $SCRIPT_DIR aliases but writes to /tmp
# Expected: no no-script-dir-write violations

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"

TMPDIR=$(mktemp -d)
trap "rm -rf '$TMPDIR'" EXIT

# These should NOT be flagged (reading from FIXTURES_DIR, writing to /tmp):
cp "$FIXTURES_DIR/input.txt" "$TMPDIR/result.txt"
cat "$FIXTURES_DIR/template.sh" > "$TMPDIR/output.sh"
