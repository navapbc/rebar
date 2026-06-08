#!/usr/bin/env bash
# Fixture: bash test file that writes to paths derived from $SCRIPT_DIR via aliases
# Expected: triggers no-script-dir-write violations

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="$SCRIPT_DIR/fixtures"
OUTPUT_DIR="$SCRIPT_DIR/output"

# These should all be caught (writing to SCRIPT_DIR aliases):
echo "data" > "$FIXTURES_DIR/result.txt"
echo "more" >> "$OUTPUT_DIR/log.txt"
touch "$FIXTURES_DIR/sentinel"
mkdir -p "$FIXTURES_DIR/subdir"
