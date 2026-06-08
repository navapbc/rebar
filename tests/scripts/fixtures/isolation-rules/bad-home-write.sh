#!/usr/bin/env bash
# Fixture: bash test file that writes to $HOME/~ without temp override
# Expected: triggers no-home-write violations

# These should all be caught:
echo "data" > $HOME/.config
echo "data" >> $HOME/.bashrc
cp file ~/backup
mkdir -p $HOME/.local
echo "test" > ~/Documents/output.txt
cat something >> $HOME/results.log
