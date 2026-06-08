"""Parity test: jq _JQ_REDUCE programs in ticket-lib-api.sh must match Python schema.

Regression guard for bug 1858-935c: the compiled-state field list is duplicated
across Python (ticket_reducer/_state.py) and two jq _JQ_REDUCE programs in
ticket-lib-api.sh. This test parses the bash file statically to extract the
field names from each jq initial_state() definition and asserts parity with
make_initial_state() from the Python package.

If a new field is added to make_initial_state() but not to the jq programs
(or vice versa), this test will fail immediately — catching the drift that
previously required manual cross-referencing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TICKET_LIB_API = REPO_ROOT / "src" / "rebar" / "_engine" / "ticket-lib-api.sh"
SCRIPTS_DIR = REPO_ROOT / "src" / "rebar" / "_engine"

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))


def _get_python_initial_state_keys() -> set[str]:
    """Return the set of field names from make_initial_state() in _state.py."""
    from ticket_reducer._state import make_initial_state

    return set(make_initial_state().keys())


def _extract_jq_initial_state_keys(jq_program: str) -> set[str]:
    """Extract the set of field names from a jq initial_state($tid) definition.

    Parses the block between ``def initial_state($tid):`` and the closing
    ``};`` (matching braces) and extracts bare key names (``key: value`` pairs).
    """
    # Locate initial_state definition
    start_match = re.search(r"def initial_state\(\$tid\):\s*\{", jq_program)
    if not start_match:
        return set()

    # Find the matching closing brace by counting depth
    body_start = start_match.end() - 1  # position of the opening {
    depth = 0
    body_end = body_start
    for i, ch in enumerate(jq_program[body_start:], start=body_start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                body_end = i
                break

    block = jq_program[body_start : body_end + 1]

    # Extract field names: lines matching /^\s+<key>:/ or /<key>:/ inside the block
    keys: set[str] = set()
    for line in block.splitlines():
        m = re.match(r"\s+(\w+)\s*:", line)
        if m:
            key = m.group(1)
            # Skip "ticket_id" when it appears in the $tid assignment on first line
            keys.add(key)

    return keys


def _extract_all_jq_programs_from_bash(bash_path: Path) -> list[str]:
    """Extract all _JQ_REDUCE heredoc bodies from a bash file.

    Looks for patterns like:
        local _JQ_REDUCE='
        ...jq program...
        '
    and returns each program body as a string.
    """
    content = bash_path.read_text(encoding="utf-8")

    # Match local _JQ_REDUCE='...' (single-quote heredoc, possibly multiline)
    # The pattern starts after "_JQ_REDUCE='" and ends at the next standalone "'"
    programs: list[str] = []
    # Find all occurrences of _JQ_REDUCE='
    for m in re.finditer(r"_JQ_REDUCE='", content):
        start = m.end()
        # Find the closing ' on its own line (or end of block)
        # Search for \n' or \n'\n pattern
        end_match = re.search(r"\n'", content[start:])
        if end_match:
            programs.append(content[start : start + end_match.start()])

    return programs


@pytest.mark.unit
@pytest.mark.scripts
def test_all_jq_reduce_programs_match_python_schema() -> None:
    """All _JQ_REDUCE jq programs in ticket-lib-api.sh must define the same
    initial_state fields as Python's make_initial_state().

    Regression guard for bug 1858-935c: silent drift when a new field is added
    to the Python schema but not to the jq programs (or vice versa).
    """
    assert TICKET_LIB_API.exists(), (
        f"ticket-lib-api.sh not found at {TICKET_LIB_API}. "
        "Adjust REPO_ROOT if the file moved."
    )

    python_keys = _get_python_initial_state_keys()
    assert python_keys, "make_initial_state() returned empty dict — check _state.py"

    jq_programs = _extract_all_jq_programs_from_bash(TICKET_LIB_API)
    assert len(jq_programs) >= 2, (
        f"Expected at least 2 _JQ_REDUCE programs in ticket-lib-api.sh, "
        f"found {len(jq_programs)}. The parity test assumes two jq programs "
        "(ticket_show and ticket_get_file_impact). If the file structure changed, "
        "update this test."
    )

    # Fields that are intentionally absent from jq initial_state but present in
    # Python make_initial_state because they are computed post-reduction (not event-
    # sourced). These are NOT drift — they are pipeline-injected by the bash wrapper.
    # parent_status_uuid is used internally by the Python reducer for STATUS event
    # deduplication and is not part of the jq reduce path.
    jq_only_exclusions: set[str] = {"parent_status_uuid"}

    expected_jq_keys = python_keys - jq_only_exclusions

    for i, program in enumerate(jq_programs, start=1):
        jq_keys = _extract_jq_initial_state_keys(program)
        assert jq_keys, (
            f"_JQ_REDUCE program #{i} in ticket-lib-api.sh: "
            "could not extract any field names from initial_state(). "
            "Check that the program still uses the 'def initial_state($tid): { ... }' pattern."
        )

        missing_from_jq = expected_jq_keys - jq_keys
        extra_in_jq = jq_keys - python_keys

        assert not missing_from_jq, (
            f"_JQ_REDUCE program #{i} in ticket-lib-api.sh is missing fields "
            f"that are present in Python make_initial_state(): {sorted(missing_from_jq)}. "
            f"Bug 1858-935c: add these fields to the jq initial_state() definition. "
            f"Python keys: {sorted(python_keys)}. "
            f"jq keys: {sorted(jq_keys)}."
        )

        assert not extra_in_jq, (
            f"_JQ_REDUCE program #{i} in ticket-lib-api.sh has extra fields "
            f"not present in Python make_initial_state(): {sorted(extra_in_jq)}. "
            f"Add these fields to make_initial_state() in _state.py, or remove "
            f"them from the jq program if they are not part of the schema. "
            f"Python keys: {sorted(python_keys)}. "
            f"jq keys: {sorted(jq_keys)}."
        )


@pytest.mark.unit
@pytest.mark.scripts
def test_jq_editable_keys_include_alias() -> None:
    """All _JQ_REDUCE programs must include 'alias' in their editable-keys list.

    Regression guard for bug 1858-935c: the second _JQ_REDUCE in ticket_get_file_impact
    was missing 'alias' from the inline _editable_keys list, causing EDIT events
    that set alias to be silently dropped when reducing via the file_impact path.
    """
    content = TICKET_LIB_API.read_text(encoding="utf-8")

    # Find all _editable_keys definitions (both as jq def and as inline array)
    # Pattern 1: def _editable_keys: [...]
    def_matches = list(
        re.finditer(r"def _editable_keys:\s*\[([^\]]+)\]", content, re.DOTALL)
    )
    # Pattern 2: inline array literal in elif condition (no def, just [...])
    inline_matches = list(
        re.finditer(r'\[\["([^"]+(?:","[^"]+)*)"\[\] == \$field\.key\]', content)
    )

    all_key_lists: list[tuple[str, str]] = []
    for m in def_matches:
        all_key_lists.append(("def _editable_keys", m.group(1)))
    for m in inline_matches:
        all_key_lists.append(("inline editable-keys array", m.group(0)))

    # If all definitions are now using def _editable_keys (consolidated),
    # we just need at least one definition and it must contain "alias".
    assert all_key_lists, (
        "Could not find any _editable_keys definition in ticket-lib-api.sh. "
        "Expected 'def _editable_keys: [...]' or inline array in elif condition."
    )

    for label, key_list in all_key_lists:
        assert "alias" in key_list, (
            f"_editable_keys ({label}) in ticket-lib-api.sh does not include 'alias'. "
            f"Bug 1858-935c: EDIT events setting alias will be silently dropped "
            f"when reducing via this path. Add 'alias' to the key list. "
            f"Key list found: {key_list!r}"
        )
