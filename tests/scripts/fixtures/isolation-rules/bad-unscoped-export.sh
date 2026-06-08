#!/usr/bin/env bash
# Fixture: bad test file with unscoped exports (no containment)

export FOO=bar
export BAZ="hello world"

run_tests() {
    echo "running tests"
}

export ANOTHER_VAR=123
