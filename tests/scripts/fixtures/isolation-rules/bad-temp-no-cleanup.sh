#!/usr/bin/env bash
# Fixture: bash test file that uses mktemp without trap EXIT cleanup
# Expected: triggers no-temp-without-cleanup violation

TMPDIR=$(mktemp -d)

echo "doing work in $TMPDIR"

# No trap EXIT — this is the violation
