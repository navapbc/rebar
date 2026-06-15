"""Library-interface tests (rebar package, in-process).

Covers behaviors specific to the Python library surface: typed exceptions, the
native read re-exports, and the fsck/fsck-recover write path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import rebar


def test_fsck_runs(rebar_repo: Path) -> None:
    """fsck() (no recovery) returns the engine's check output."""
    out = rebar.fsck(repo_root=str(rebar_repo))
    assert "fsck" in out.lower()


def test_fsck_recover(rebar_repo: Path) -> None:
    """fsck(recover=True) must run the recovery path, not fail with an unknown
    subcommand.

    Regression: the library maps recover=True -> the 'fsck-recover' subcommand,
    but the dispatcher had no such arm, so it raised
    RebarError("unknown subcommand 'fsck-recover'").
    """
    out = rebar.fsck(recover=True, repo_root=str(rebar_repo))
    assert isinstance(out, str)


def test_concurrency_error_typed(rebar_repo: Path) -> None:
    """A transition with a valid-but-stale current_status raises ConcurrencyError
    (engine exit 10), not a generic RebarError."""
    tid = rebar.create_ticket("task", "T", repo_root=str(rebar_repo))
    with pytest.raises(rebar.ConcurrencyError) as exc:
        # Ticket is 'open'; claim a valid-but-wrong current status.
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert exc.value.returncode == 10


def test_version_matches_package_metadata() -> None:
    """rebar.__version__ is derived from the installed package metadata, so it
    cannot drift from the actual distribution version."""
    import importlib.metadata

    assert rebar.__version__ == importlib.metadata.version("nava-rebar")


def test_clarity_check_missing_ticket_schema_conformant(rebar_repo: Path) -> None:
    """clarity_check on a nonexistent id returns a structured failure dict that
    validates against clarity_result.schema.json and ClarityResultOut — like its
    sibling gates, it does NOT return the old {output, passed} shape."""
    from rebar import schemas

    res = rebar.clarity_check("no-such-ticket-xyz", repo_root=str(rebar_repo))
    assert isinstance(res, dict)
    assert "output" not in res
    assert set(("score", "verdict", "threshold")) <= set(res)
    assert res["passed"] is False
    # Validates against the canonical schema.
    schemas.validator(schemas.CLARITY_RESULT).validate(res)
    # And against the MCP output model.
    from rebar.mcp_server import ClarityResultOut

    ClarityResultOut.model_validate(res)


def test_sibling_gates_missing_ticket_structured(rebar_repo: Path) -> None:
    """check_ac/quality_check on a missing id already return structured fail
    dicts; assert clarity_check is now consistent with them."""
    for fn in (rebar.check_ac, rebar.quality_check):
        res = fn("no-such-ticket-xyz", repo_root=str(rebar_repo))
        assert res.get("verdict") == "fail"
        assert res["passed"] is False


def test_link_docstring_lists_all_relations() -> None:
    """rebar.link's docstring must document all six canonical relations
    (sourced from the engine's CANONICAL_RELATIONS — single source of truth)."""
    import rebar  # noqa: F401
    from rebar.graph._links import CANONICAL_RELATIONS

    doc = rebar.link.__doc__ or ""
    for rel in CANONICAL_RELATIONS:
        assert rel in doc, f"library link doc missing relation {rel!r}"


def test_reconcile_docstring_names_all_mutating_modes() -> None:
    """The reconcile docstring must name every Jira-mutating mode, not just
    'live' (bootstrap-strict/bootstrap-throttle mutate too)."""
    doc = rebar.reconcile.__doc__ or ""
    for mode in ("bootstrap-strict", "bootstrap-throttle", "live"):
        assert mode in doc, f"reconcile doc missing mutating mode {mode!r}"


def test_native_reexports_importable() -> None:
    """The stdlib-only native read API is re-exported on the package."""
    for name in (
        "reduce_all_tickets",
        "reduce_ticket",
        "to_llm",
        "find_inbound_relationships",
        "apply_ticket_filters",
    ):
        assert callable(getattr(rebar, name)), name
