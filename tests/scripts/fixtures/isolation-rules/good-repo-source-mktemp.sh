#!/usr/bin/env bash
# Fixture: mktemp creating files outside the repo tree (GOOD — proper isolation)
_TEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/test-auto-format-XXXXXX")
trap 'rm -rf "$_TEST_DIR"' EXIT
mkdir -p "$_TEST_DIR/app/src"
echo "x = 1" > "$_TEST_DIR/app/src/fake_test.py"
