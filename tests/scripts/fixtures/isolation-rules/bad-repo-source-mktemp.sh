#!/usr/bin/env bash
# Fixture: mktemp creating files inside $REPO_ROOT (BAD)
REPO_ROOT="$(git rev-parse --show-toplevel)"
trap '_cleanup' EXIT
_cleanup() { rm -f "$_F"; }
_F=$(mktemp "$REPO_ROOT/app/src/fake_test_XXXXXX.py")
echo "x = 1" > "$_F"
