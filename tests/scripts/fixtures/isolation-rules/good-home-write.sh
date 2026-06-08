#!/usr/bin/env bash
# Fixture: bash test file that uses HOME safely with temp override
# Expected: passes no-home-write rule

# Override HOME with temp directory before using it
HOME=$(mktemp -d)
trap 'rm -rf "$HOME"' EXIT

echo "data" > $HOME/.config
cp file $HOME/backup
mkdir -p $HOME/.local

# This uses ~ but HOME was already overridden above
echo "test" > ~/Documents/output.txt

# This has a suppression comment
echo "real home" > $ORIGINAL_HOME/.config  # isolation-ok: intentional write for setup
