#!/usr/bin/env bash
# Fixture: regression test file with a known violation (unscoped export)
# Used by test-isolation-check.sh to verify the harness catches violations.
# This file intentionally contains an isolation violation — do NOT fix it.

export REGRESSION_TEST_VAR=intentional_violation
