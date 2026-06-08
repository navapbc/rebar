#!/usr/bin/env bash
# Fixture: good test file with properly scoped exports

# Subshell containment
(
    export FOO=bar
    run_test_in_subshell
)

# Save/restore pattern
_OLD_BAZ="${BAZ:-}"
export BAZ="hello world"
run_tests
export BAZ="$_OLD_BAZ"

# Function-scoped (local + export in subshell)
test_with_env() {
    (
        export INNER_VAR=value
        run_inner_test
    )
}
