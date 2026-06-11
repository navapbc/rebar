"""RED tests for ticket_reducer/llm_format.py (to_llm package integration).

These tests are RED — they test functionality that does not yet exist:
    ticket_reducer/llm_format.py has not been created yet.

All test functions MUST FAIL before ticket_reducer/llm_format.py is implemented.

Tests:
  (1) test_to_llm_importable_from_package
      — from ticket_reducer.llm_format import to_llm; assert callable(to_llm)
  (2) test_to_llm_key_mapping_via_package
      — same key-mapping assertions as test_ticket_llm_format.py, but importing
        from the package (not importlib); verifies the module re-exports the
        correct public interface.
  (3) test_to_llm_omits_none_via_package
      — None values and empty lists are omitted by the package-imported to_llm.
  (4) test_to_llm_importable_from_top_level_package
      — from ticket_reducer import to_llm (tests __init__.py re-export).

Run: python3 -m pytest tests/scripts/test_ticket_reducer_llm_format.py
All tests must return non-zero until ticket_reducer/llm_format.py is created and
ticket_reducer/__init__.py re-exports to_llm.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure ticket_reducer package is importable regardless of invocation directory.
_SCRIPTS_DIR = str(Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


# ---------------------------------------------------------------------------
# Test 1: to_llm is importable from ticket_reducer.llm_format sub-module
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_to_llm_importable_from_package() -> None:
    """from ticket_reducer.llm_format import to_llm must succeed and return a callable.

    RED: ticket_reducer/llm_format.py does not exist yet; the import will raise
    ModuleNotFoundError until the module is created.
    """
    from ticket_reducer.llm_format import to_llm  # noqa: PLC0415 — intentional RED import

    assert callable(to_llm), (
        "to_llm imported from ticket_reducer.llm_format must be callable"
    )


# ---------------------------------------------------------------------------
# Test 2: to_llm key mapping via package import
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_to_llm_key_mapping_via_package() -> None:
    """to_llm imported from ticket_reducer.llm_format must produce the expected
    abbreviated key mapping for all documented fields.

    Key mapping contract (mirrors ticket-llm-format.py):
        ticket_id   → id
        ticket_type → t
        title       → ttl
        status      → st
        author      → au
        parent_id   → pid
        priority    → pr
        assignee    → asn
        description → desc
        comments    → cm
        deps        → dp
        conflicts   → cf

    RED: ticket_reducer/llm_format.py does not exist yet.
    """
    from ticket_reducer.llm_format import to_llm  # noqa: PLC0415

    state = {
        "ticket_id": "abc-123",
        "ticket_type": "story",
        "title": "My title",
        "status": "open",
        "author": "alice",
        "parent_id": "epic-1",
        "priority": 2,
        "assignee": "bob",
        "description": "A short description",
        "comments": [{"body": "hi", "author": "bob"}],
        "deps": [{"target_id": "t-1", "relation": "blocks"}],
        "conflicts": ["file.py"],
    }
    result = to_llm(state)

    assert result.get("id") == "abc-123", "ticket_id must be mapped to 'id'"
    assert "ticket_id" not in result, (
        "original 'ticket_id' key must not appear in output"
    )
    assert result.get("t") == "story", "ticket_type must be mapped to 't'"
    assert result.get("ttl") == "My title", "title must be mapped to 'ttl'"
    assert result.get("st") == "open", "status must be mapped to 'st'"
    assert result.get("au") == "alice", "author must be mapped to 'au'"
    assert result.get("pid") == "epic-1", "parent_id must be mapped to 'pid'"
    assert result.get("pr") == 2, "priority must be mapped to 'pr'"
    assert result.get("asn") == "bob", "assignee must be mapped to 'asn'"
    assert result.get("desc") == "A short description", (
        "description must be mapped to 'desc'"
    )
    assert "cm" in result, "comments must be mapped to 'cm'"
    assert "comments" not in result, "original 'comments' key must not appear in output"
    assert "dp" in result, "deps must be mapped to 'dp'"
    assert "deps" not in result, "original 'deps' key must not appear in output"
    assert result.get("cf") == ["file.py"], "conflicts must be mapped to 'cf'"


# ---------------------------------------------------------------------------
# Test 3: to_llm omits None values and empty lists via package import
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_to_llm_omits_none_via_package() -> None:
    """to_llm imported from ticket_reducer.llm_format must omit None values,
    empty lists, and the created_at / env_id fields.

    RED: ticket_reducer/llm_format.py does not exist yet.
    """
    from ticket_reducer.llm_format import to_llm  # noqa: PLC0415

    state = {
        "ticket_id": "dso-001",
        "ticket_type": "story",
        "title": "As a user, I can do things",
        "status": "open",
        "author": "alice",
        "parent_id": None,  # → must be omitted (None)
        "assignee": None,  # → must be omitted (None)
        "priority": None,  # → must be omitted (None)
        "description": None,  # → must be omitted (None)
        "created_at": "2026-01-01T00:00:00Z",  # → must be omitted (OMIT_KEYS)
        "env_id": "local",  # → must be omitted (OMIT_KEYS)
        "comments": [],  # → must be omitted (empty list)
        "deps": [],  # → must be omitted (empty list)
    }
    result = to_llm(state)

    # Required fields must be present
    assert result.get("id") == "dso-001"
    assert result.get("t") == "story"
    assert result.get("ttl") == "As a user, I can do things"
    assert result.get("st") == "open"
    assert result.get("au") == "alice"

    # None values must be omitted
    assert "pid" not in result, (
        "parent_id=None must be omitted (no 'pid' key in output)"
    )
    assert "asn" not in result, "assignee=None must be omitted (no 'asn' key in output)"
    assert "pr" not in result, "priority=None must be omitted (no 'pr' key in output)"
    assert "desc" not in result, (
        "description=None must be omitted (no 'desc' key in output)"
    )

    # OMIT_KEYS fields must be dropped entirely
    assert "created_at" not in result, "created_at must be omitted (in OMIT_KEYS)"
    assert "env_id" not in result, "env_id must be omitted (in OMIT_KEYS)"

    # Empty lists must be omitted
    assert "cm" not in result, "comments=[] must be omitted (empty list)"
    assert "dp" not in result, "deps=[] must be omitted (empty list)"


# ---------------------------------------------------------------------------
# Test 4: to_llm importable from top-level ticket_reducer package (__init__.py re-export)
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.scripts
def test_to_llm_importable_from_top_level_package() -> None:
    """from ticket_reducer import to_llm must succeed and return a callable.

    This tests that ticket_reducer/__init__.py re-exports to_llm from the
    llm_format sub-module, making it accessible as ticket_reducer.to_llm.

    RED: ticket_reducer/__init__.py does not yet re-export to_llm; the import
    will raise ImportError until __init__.py is updated.
    """
    from ticket_reducer import to_llm  # noqa: PLC0415 — intentional RED import

    assert callable(to_llm), (
        "to_llm imported from ticket_reducer (top-level) must be callable"
    )

    # Smoke-check: verify it is the same function as the one from the sub-module
    from ticket_reducer.llm_format import to_llm as to_llm_direct  # noqa: PLC0415

    assert to_llm is to_llm_direct, (
        "ticket_reducer.to_llm must be the same object as "
        "ticket_reducer.llm_format.to_llm (re-export, not a copy)"
    )
