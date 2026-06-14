#!/usr/bin/env bash
# tests/scripts/test-ticket-clarity-check.sh
# RED tests for src/rebar/_engine/ticket-clarity-check.sh scoring heuristic.
#
# Tests use --stdin flag to pipe JSON ticket fixtures directly to the script,
# bypassing the live ticket CLI. Each test creates a temp JSON fixture with
# ticket_type and description fields, pipes via stdin, asserts exit code and
# JSON output fields: score, verdict.
#
# Test cases (12):
#   test_empty_description          — empty desc -> score 0, exit 1
#   test_short_description          — <100 chars -> score < 5, exit 1
#   test_epic_with_success_criteria — "## Success Criteria" -> +2 type-specific
#   test_bug_with_repro_steps       — "## Reproduction Steps" -> +2 type-specific
#   test_task_with_acceptance_criteria — "## Acceptance Criteria" -> +2 type-specific
#   test_story_with_why_what        — "## Why" + "## What" -> +2 type-specific
#   test_length_200                 — >=200 chars -> +1
#   test_length_500                 — >=500 chars -> +1 additional
#   test_section_headers            — ## lines -> +1
#   test_bullet_lists               — - lines -> +1
#   test_threshold_override         — custom threshold via temp config file -> verdict changes
#   test_threshold_minimum          — threshold=0 in config -> script uses minimum 1
#
# Usage: bash tests/scripts/test-ticket-clarity-check.sh
# RED STATE: All tests currently fail because ticket-clarity-check.sh does not
# yet exist. They will pass (GREEN) after the script is implemented.

# NOTE: -e is intentionally omitted — test functions return non-zero by design
# (they assert against unimplemented features). -e would abort the runner.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
SUT="$REPO_ROOT/src/rebar/_engine/ticket-clarity-check.sh"

source "$REPO_ROOT/tests/lib/assert.sh"

# ── Temp dir cleanup on exit ──────────────────────────────────────────────────
_CLEANUP_DIRS=()
_cleanup() {
    for d in "${_CLEANUP_DIRS[@]:-}"; do
        rm -rf "$d"
    done
}
trap _cleanup EXIT

echo "=== test-ticket-clarity-check.sh ==="

# ── Helper: build a minimal ticket JSON fixture ───────────────────────────────
# Usage: _make_ticket_json <ticket_type> <description>
_make_ticket_json() {
    local ticket_type="$1"
    local description="$2"
    python3 - "$ticket_type" "$description" <<'PYEOF'
import json, sys
t = {
    "ticket_id": "test-0001",
    "ticket_type": sys.argv[1],
    "title": "Test ticket",
    "status": "open",
    "description": sys.argv[2],
    "comments": []
}
print(json.dumps(t))
PYEOF
}

# ── Helper: run SUT with a JSON fixture via --stdin ───────────────────────────
# Usage: _run_sut <json_string> [extra_args...]
# Sets _SUT_OUTPUT and returns exit code from the single invocation.
# Prefer _run_sut_capture for tests that need both output and exit code.
_run_sut() {
    local json_input="$1"
    shift
    local extra_args=("$@")
    local exit_code=0
    _SUT_OUTPUT=$(echo "$json_input" | bash "$SUT" --stdin "${extra_args[@]}" 2>/dev/null) || exit_code=$?
    echo "$_SUT_OUTPUT"
    return $exit_code
}

# ── Helper: run SUT and capture output + exit code separately ────────────────
# Usage: _run_sut_capture <json_string> [extra_args...]
# Sets: _SUT_OUTPUT, _SUT_EXIT
_run_sut_capture() {
    local json_input="$1"
    shift
    local extra_args=("$@")
    _SUT_EXIT=0
    _SUT_OUTPUT=$(echo "$json_input" | bash "$SUT" --stdin ${extra_args[@]+"${extra_args[@]}"} 2>/dev/null) || _SUT_EXIT=$?
}

# ── Helper: extract a JSON field from SUT output ─────────────────────────────
_get_field() {
    local json="$1" field="$2"
    python3 -c "import json,sys; d=json.loads(sys.argv[1]); print(d.get('$field',''))" "$json" 2>/dev/null || echo ""
}

# ── Helper: compare two numbers ──────────────────────────────────────────────
# Uses bash arithmetic to avoid int() parsing failures on empty/non-numeric input.
# Inputs are coerced to integers via ${1:-0} defaulting.
_lt()     { (( ${1:-0} < ${2:-0} )); }
_ge()     { (( ${1:-0} >= ${2:-0} )); }
_eq_num() { (( ${1:-0} == ${2:-0} )); }

# ── Guard: skip all tests if SUT does not exist (mark RED) ───────────────────
if [[ ! -f "$SUT" ]]; then
    echo "RED: ticket-clarity-check.sh does not exist yet — all tests fail" >&2
    assert_eq "ticket-clarity-check.sh exists" "exists" "not_found"
    print_summary
fi

# ── test_empty_description ────────────────────────────────────────────────────
# Empty description must produce score=0 and exit 1 (clarity failure)
test_empty_description() {
    _snapshot_fail
    local json
    json=$(_make_ticket_json "task" "")
    _run_sut_capture "$json"

    assert_eq "test_empty_description: exits 1 for empty description" "1" "$_SUT_EXIT"

    local score
    score=$(_get_field "$_SUT_OUTPUT" "score")
    assert_eq "test_empty_description: score is 0 for empty description" "0" "$score"

    assert_pass_if_clean "test_empty_description"
}

# ── test_short_description ────────────────────────────────────────────────────
# Description <100 chars must produce score < 5 and exit 1
test_short_description() {
    _snapshot_fail
    # 50 chars — below 100 char threshold
    local short_desc
    short_desc="Short description that is under one hundred characters."
    local json
    json=$(_make_ticket_json "task" "$short_desc")
    _run_sut_capture "$json"

    assert_eq "test_short_description: exits 1 for short description" "1" "$_SUT_EXIT"

    local score
    score=$(_get_field "$_SUT_OUTPUT" "score")
    local score_low
    if _lt "${score:-0}" "5"; then
        score_low="yes"
    else
        score_low="no"
    fi
    assert_eq "test_short_description: score < 5 for short description" "yes" "$score_low"

    assert_pass_if_clean "test_short_description"
}

# ── test_epic_with_success_criteria ──────────────────────────────────────────
# Epic ticket with "## Success Criteria" section must earn +2 type-specific bonus
test_epic_with_success_criteria() {
    _snapshot_fail
    # Build a base description of >=200 chars with section headers and bullets
    # to ensure base score is positive, then verify type-specific section is counted
    local desc
    desc=$(python3 -c "
desc = 'This epic covers a major feature area that requires careful planning and execution. ' * 3
desc += '\n\n## Overview\nThis section provides context about the epic goals and objectives.\n'
desc += '- Goal 1: achieve integration\n- Goal 2: improve reliability\n- Goal 3: reduce toil\n'
desc += '\n## Success Criteria\n- [ ] All integration tests pass\n- [ ] No regressions in staging\n'
print(desc)
")

    local json
    json=$(_make_ticket_json "epic" "$desc")

    # Run WITHOUT type-specific section to get base score
    local base_desc
    base_desc=$(python3 -c "
desc = 'This epic covers a major feature area that requires careful planning and execution. ' * 3
desc += '\n\n## Overview\nThis section provides context about the epic goals and objectives.\n'
desc += '- Goal 1: achieve integration\n- Goal 2: improve reliability\n- Goal 3: reduce toil\n'
print(desc)
")
    local base_json
    base_json=$(_make_ticket_json "epic" "$base_desc")

    _run_sut_capture "$json"
    local score_with
    score_with=$(_get_field "$_SUT_OUTPUT" "score")

    _run_sut_capture "$base_json"
    local score_without
    score_without=$(_get_field "$_SUT_OUTPUT" "score")

    # score_with must be > score_without (type-specific bonus applied)
    local bonus_applied
    if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)" \
        "${score_with:-0}" "${score_without:-0}" 2>/dev/null; then
        bonus_applied="yes"
    else
        bonus_applied="no"
    fi
    assert_eq "test_epic_with_success_criteria: Success Criteria section adds bonus to epic score" "yes" "$bonus_applied"

    assert_pass_if_clean "test_epic_with_success_criteria"
}

# ── test_bug_with_repro_steps ─────────────────────────────────────────────────
# Bug ticket with "## Reproduction Steps" section must earn +2 type-specific bonus
test_bug_with_repro_steps() {
    _snapshot_fail
    local desc_with_repro
    desc_with_repro=$(python3 -c "
desc = 'The system crashes when the user submits the form with an empty required field. ' * 3
desc += '\n\n## Context\nThis was reported by QA during regression testing on the staging environment.\n'
desc += '- Severity: P1\n- Frequency: always\n- Affected versions: 1.2.x\n'
desc += '\n## Reproduction Steps\n1. Open the form\n2. Leave required field blank\n3. Submit\n'
print(desc)
")
    local desc_without_repro
    desc_without_repro=$(python3 -c "
desc = 'The system crashes when the user submits the form with an empty required field. ' * 3
desc += '\n\n## Context\nThis was reported by QA during regression testing on the staging environment.\n'
desc += '- Severity: P1\n- Frequency: always\n- Affected versions: 1.2.x\n'
print(desc)
")

    local json_with json_without
    json_with=$(_make_ticket_json "bug" "$desc_with_repro")
    json_without=$(_make_ticket_json "bug" "$desc_without_repro")

    _run_sut_capture "$json_with"
    local score_with
    score_with=$(_get_field "$_SUT_OUTPUT" "score")

    _run_sut_capture "$json_without"
    local score_without
    score_without=$(_get_field "$_SUT_OUTPUT" "score")

    local bonus_applied
    if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)" \
        "${score_with:-0}" "${score_without:-0}" 2>/dev/null; then
        bonus_applied="yes"
    else
        bonus_applied="no"
    fi
    assert_eq "test_bug_with_repro_steps: Reproduction Steps section adds bonus to bug score" "yes" "$bonus_applied"

    assert_pass_if_clean "test_bug_with_repro_steps"
}

# ── test_task_with_acceptance_criteria ───────────────────────────────────────
# Task ticket with "## Acceptance Criteria" section must earn +2 type-specific bonus
test_task_with_acceptance_criteria() {
    _snapshot_fail
    local desc_with_ac
    desc_with_ac=$(python3 -c "
desc = 'Implement the new caching layer for the API responses to improve latency. ' * 3
desc += '\n\n## Background\nThe API is currently too slow under load due to repeated DB queries.\n'
desc += '- Current p99: 2s\n- Target p99: 200ms\n- Cache TTL: 60s\n'
desc += '\n## Acceptance Criteria\n- [ ] Cache returns hits within 10ms\n- [ ] Cache miss falls through to DB\n'
print(desc)
")
    local desc_without_ac
    desc_without_ac=$(python3 -c "
desc = 'Implement the new caching layer for the API responses to improve latency. ' * 3
desc += '\n\n## Background\nThe API is currently too slow under load due to repeated DB queries.\n'
desc += '- Current p99: 2s\n- Target p99: 200ms\n- Cache TTL: 60s\n'
print(desc)
")

    local json_with json_without
    json_with=$(_make_ticket_json "task" "$desc_with_ac")
    json_without=$(_make_ticket_json "task" "$desc_without_ac")

    _run_sut_capture "$json_with"
    local score_with
    score_with=$(_get_field "$_SUT_OUTPUT" "score")

    _run_sut_capture "$json_without"
    local score_without
    score_without=$(_get_field "$_SUT_OUTPUT" "score")

    local bonus_applied
    if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)" \
        "${score_with:-0}" "${score_without:-0}" 2>/dev/null; then
        bonus_applied="yes"
    else
        bonus_applied="no"
    fi
    assert_eq "test_task_with_acceptance_criteria: Acceptance Criteria section adds bonus to task score" "yes" "$bonus_applied"

    assert_pass_if_clean "test_task_with_acceptance_criteria"
}

# ── test_story_with_why_what ──────────────────────────────────────────────────
# Story ticket with "## Why" + "## What" sections must earn +2 type-specific bonus
test_story_with_why_what() {
    _snapshot_fail
    local desc_with_why_what
    desc_with_why_what=$(python3 -c "
desc = 'As a developer I want to have a unified interface for the configuration system. ' * 3
desc += '\n\n## Context\nCurrently configs are scattered across multiple files and formats.\n'
desc += '- Affects: all services\n- Risk: medium\n- Stakeholders: platform team\n'
desc += '\n## Why\nConfiguration inconsistencies cause bugs and slow onboarding.\n'
desc += '\n## What\nIntroduce a single read-config.sh that all scripts call.\n'
print(desc)
")
    local desc_without
    desc_without=$(python3 -c "
desc = 'As a developer I want to have a unified interface for the configuration system. ' * 3
desc += '\n\n## Context\nCurrently configs are scattered across multiple files and formats.\n'
desc += '- Affects: all services\n- Risk: medium\n- Stakeholders: platform team\n'
print(desc)
")

    local json_with json_without
    json_with=$(_make_ticket_json "story" "$desc_with_why_what")
    json_without=$(_make_ticket_json "story" "$desc_without")

    _run_sut_capture "$json_with"
    local score_with
    score_with=$(_get_field "$_SUT_OUTPUT" "score")

    _run_sut_capture "$json_without"
    local score_without
    score_without=$(_get_field "$_SUT_OUTPUT" "score")

    local bonus_applied
    if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)" \
        "${score_with:-0}" "${score_without:-0}" 2>/dev/null; then
        bonus_applied="yes"
    else
        bonus_applied="no"
    fi
    assert_eq "test_story_with_why_what: Why+What sections add bonus to story score" "yes" "$bonus_applied"

    assert_pass_if_clean "test_story_with_why_what"
}

# ── test_length_200 ───────────────────────────────────────────────────────────
# Description >=200 chars must contribute +1 length bonus vs <200 chars
test_length_200() {
    _snapshot_fail
    # Build exactly 200-char description (no headers/bullets to isolate length signal)
    local desc_200
    desc_200=$(python3 -c "print('x' * 200)")
    local desc_50
    desc_50=$(python3 -c "print('x' * 50)")

    local json_200 json_50
    json_200=$(_make_ticket_json "task" "$desc_200")
    json_50=$(_make_ticket_json "task" "$desc_50")

    _run_sut_capture "$json_200"
    local score_200
    score_200=$(_get_field "$_SUT_OUTPUT" "score")

    _run_sut_capture "$json_50"
    local score_50
    score_50=$(_get_field "$_SUT_OUTPUT" "score")

    # score_200 must be > score_50 (length bonus applied at >=200)
    local length_bonus
    if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)" \
        "${score_200:-0}" "${score_50:-0}" 2>/dev/null; then
        length_bonus="yes"
    else
        length_bonus="no"
    fi
    assert_eq "test_length_200: >=200 char description scores higher than <200 chars" "yes" "$length_bonus"

    assert_pass_if_clean "test_length_200"
}

# ── test_length_500 ───────────────────────────────────────────────────────────
# Description >=500 chars must contribute an additional +1 bonus beyond 200-char bonus
test_length_500() {
    _snapshot_fail
    # 500 chars vs exactly 200 chars — isolate the second length tier
    local desc_500
    desc_500=$(python3 -c "print('x' * 500)")
    local desc_200
    desc_200=$(python3 -c "print('x' * 200)")

    local json_500 json_200
    json_500=$(_make_ticket_json "task" "$desc_500")
    json_200=$(_make_ticket_json "task" "$desc_200")

    _run_sut_capture "$json_500"
    local score_500
    score_500=$(_get_field "$_SUT_OUTPUT" "score")

    _run_sut_capture "$json_200"
    local score_200
    score_200=$(_get_field "$_SUT_OUTPUT" "score")

    local second_length_bonus
    if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)" \
        "${score_500:-0}" "${score_200:-0}" 2>/dev/null; then
        second_length_bonus="yes"
    else
        second_length_bonus="no"
    fi
    assert_eq "test_length_500: >=500 char description scores higher than 200-char description" "yes" "$second_length_bonus"

    assert_pass_if_clean "test_length_500"
}

# ── test_section_headers ──────────────────────────────────────────────────────
# Description with ## section headers must earn +1 structure bonus
test_section_headers() {
    _snapshot_fail
    # Same total length, with vs without section headers
    local desc_with_headers
    desc_with_headers=$(python3 -c "
desc = 'x' * 150
desc += '\n\n## Overview\n' + 'y' * 100
print(desc)
")
    local desc_no_headers
    desc_no_headers=$(python3 -c "print('x' * 280)")

    local json_with json_without
    json_with=$(_make_ticket_json "task" "$desc_with_headers")
    json_without=$(_make_ticket_json "task" "$desc_no_headers")

    _run_sut_capture "$json_with"
    local score_with
    score_with=$(_get_field "$_SUT_OUTPUT" "score")

    _run_sut_capture "$json_without"
    local score_without
    score_without=$(_get_field "$_SUT_OUTPUT" "score")

    local header_bonus
    if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)" \
        "${score_with:-0}" "${score_without:-0}" 2>/dev/null; then
        header_bonus="yes"
    else
        header_bonus="no"
    fi
    assert_eq "test_section_headers: ## headers add structure bonus to score" "yes" "$header_bonus"

    assert_pass_if_clean "test_section_headers"
}

# ── test_bullet_lists ─────────────────────────────────────────────────────────
# Description with bullet list items (- lines) must earn +1 structure bonus
test_bullet_lists() {
    _snapshot_fail
    local desc_with_bullets
    desc_with_bullets=$(python3 -c "
desc = 'x' * 200
desc += '\n- item one\n- item two\n- item three\n'
print(desc)
")
    local desc_no_bullets
    desc_no_bullets=$(python3 -c "print('x' * 240)")

    local json_with json_without
    json_with=$(_make_ticket_json "task" "$desc_with_bullets")
    json_without=$(_make_ticket_json "task" "$desc_no_bullets")

    _run_sut_capture "$json_with"
    local score_with
    score_with=$(_get_field "$_SUT_OUTPUT" "score")

    _run_sut_capture "$json_without"
    local score_without
    score_without=$(_get_field "$_SUT_OUTPUT" "score")

    local bullet_bonus
    if python3 -c "import sys; sys.exit(0 if int(sys.argv[1]) > int(sys.argv[2]) else 1)" \
        "${score_with:-0}" "${score_without:-0}" 2>/dev/null; then
        bullet_bonus="yes"
    else
        bullet_bonus="no"
    fi
    assert_eq "test_bullet_lists: bullet list items add structure bonus to score" "yes" "$bullet_bonus"

    assert_pass_if_clean "test_bullet_lists"
}

# ── test_threshold_override ───────────────────────────────────────────────────
# Custom threshold via temp config file must change verdict
# A description that passes at threshold=3 must fail at threshold=10
test_threshold_override() {
    _snapshot_fail

    # Build a description that scores around 4-5 (length + headers + bullets)
    local moderate_desc
    # Includes an Acceptance Criteria block: clarity-check requires the AC floor
    # for a pass on every type (one vocabulary with check-ac), so this fixture
    # isolates the THRESHOLD behaviour rather than tripping the AC floor.
    moderate_desc=$(python3 -c "
desc = 'x' * 250
desc += '\n\n## Background\n' + 'y' * 50 + '\n'
desc += '- item one\n- item two\n'
desc += '\n## Acceptance Criteria\n- [ ] criterion one\n'
print(desc)
")

    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")

    # Config file with low threshold — should pass
    local low_conf="$tmpdir/low-threshold.conf"
    printf 'ticket_clarity.threshold=3\n' > "$low_conf"

    # Config file with high threshold — should fail
    local high_conf="$tmpdir/high-threshold.conf"
    printf 'ticket_clarity.threshold=10\n' > "$high_conf"

    local json
    json=$(_make_ticket_json "task" "$moderate_desc")

    # Run with low threshold
    _SUT_EXIT=0
    _SUT_OUTPUT=$(echo "$json" | bash "$SUT" --stdin --config "$low_conf" 2>/dev/null) || _SUT_EXIT=$?
    local exit_low=$_SUT_EXIT
    local verdict_low
    verdict_low=$(_get_field "$_SUT_OUTPUT" "verdict")

    # Run with high threshold
    _SUT_EXIT=0
    _SUT_OUTPUT=$(echo "$json" | bash "$SUT" --stdin --config "$high_conf" 2>/dev/null) || _SUT_EXIT=$?
    local exit_high=$_SUT_EXIT
    local verdict_high
    verdict_high=$(_get_field "$_SUT_OUTPUT" "verdict")

    # With low threshold: should exit 0 (pass)
    assert_eq "test_threshold_override: low threshold exits 0 (pass)" "0" "$exit_low"
    # With high threshold: should exit 1 (fail)
    assert_eq "test_threshold_override: high threshold exits 1 (fail)" "1" "$exit_high"

    # Verdicts must differ
    assert_ne "test_threshold_override: verdicts differ between low and high threshold" \
        "$verdict_low" "$verdict_high"

    assert_pass_if_clean "test_threshold_override"
}

# ── test_threshold_minimum ────────────────────────────────────────────────────
# threshold=0 in config must be treated as minimum 1 (score 0 still fails)
test_threshold_minimum() {
    _snapshot_fail

    local tmpdir
    tmpdir=$(mktemp -d)
    _CLEANUP_DIRS+=("$tmpdir")

    # Config with threshold=0 — script must enforce minimum of 1
    local zero_conf="$tmpdir/zero-threshold.conf"
    printf 'ticket_clarity.threshold=0\n' > "$zero_conf"

    # Empty description — score=0 should still fail even with threshold=0
    local json
    json=$(_make_ticket_json "task" "")

    _SUT_EXIT=0
    _SUT_OUTPUT=$(echo "$json" | bash "$SUT" --stdin --config "$zero_conf" 2>/dev/null) || _SUT_EXIT=$?

    # Even with threshold=0 config, score=0 must fail (minimum threshold=1 enforced)
    assert_eq "test_threshold_minimum: threshold=0 config uses minimum 1 — score=0 exits 1" "1" "$_SUT_EXIT"

    assert_pass_if_clean "test_threshold_minimum"
}

# ── Run all tests ─────────────────────────────────────────────────────────────
test_empty_description
test_short_description
test_epic_with_success_criteria
test_bug_with_repro_steps
test_task_with_acceptance_criteria
test_story_with_why_what
test_length_200
test_length_500
test_section_headers
test_bullet_lists
test_threshold_override
test_threshold_minimum

print_summary
