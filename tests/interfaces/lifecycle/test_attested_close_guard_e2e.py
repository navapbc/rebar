"""Attestation-laundering close guard, end-to-end (bug 2f56-313f-6175-41b1).

Reproduces the live wedge from epic fb8a-7363-e406-4e36: a code-verifiable AC retagged
``[non-codebase]`` (its proof is an exact repo path/symbol) sailed through the close
because the completion verifier classifies SOLELY from the author tag (ADR-0043). The
deterministic close guard must reject it BEFORE any LLM call; legitimately external items
carrying a complete ``provenance:`` continuation line still close. Both the canonical
``[non-codebase]`` spelling and its accepted ``[operator-attested]`` alias are covered; the
block messages teach the canonical tag either way. Same harness as
test_ac_checkbox_precheck.py: monkeypatch ``rebar.llm.verify_completion``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm

_PROVENANCE = (
    "      provenance: environment=production; principal=release-operator; "
    "privilege_posture=production-equivalent; instrument=live-call — console shows green"
)


def _enable(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path, description: str) -> str:
    tid = rebar.create_ticket("task", "launder gate", description=description, repo_root=str(repo))
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    return tid


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def PASS(ticket_id, **kw):
    return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}


def _never(ticket_id, **kw):  # must NOT be called
    raise AssertionError("verify_completion was called when it must not be")


def test_mistagged_repo_verifiable_item_blocks_close(rebar_repo: Path, monkeypatch) -> None:
    """THE BUG: a tagged item citing an exact repo path must fail the close, pre-LLM."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = (
        "## Acceptance Criteria\n"
        "- [x] [operator-attested] scan scoping holds; proxy: tests/unit/test_scan_scoping.py\n"
    )
    tid = _make(rebar_repo, desc)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert ei.value.returncode == 1
    assert "tests/unit/test_scan_scoping.py" in ei.value.stderr
    assert "non-codebase" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "in_progress"


def test_tagged_item_missing_provenance_blocks_close(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "## Acceptance Criteria\n- [ ] [operator-attested] the prod deploy is confirmed live\n"
    tid = _make(rebar_repo, desc)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert "provenance" in ei.value.stderr.lower()
    assert "non-codebase" in ei.value.stderr.lower()
    assert _status(tid, rebar_repo) == "in_progress"


def test_external_item_with_provenance_still_closes(rebar_repo: Path, monkeypatch) -> None:
    """AC-4 guard: the legitimate ADR-0043 escape hatch keeps working."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = (
        "## Acceptance Criteria\n"
        "- [x] code merged\n"
        "- [ ] [operator-attested] the prod deploy is confirmed live\n"
        f"{_PROVENANCE}\n"
    )
    tid = _make(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_force_close_bypasses_the_laundering_guard(rebar_repo: Path, monkeypatch) -> None:
    """--force keeps its meaning: close WITHOUT verification (and without a signature)."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = (
        "## Acceptance Criteria\n"
        "- [x] [operator-attested] holds; proxy: tests/unit/test_scan_scoping.py\n"
    )
    tid = _make(rebar_repo, desc)
    rebar.transition(
        tid, "in_progress", "closed", force="operator says so", repo_root=str(rebar_repo)
    )
    assert _status(tid, rebar_repo) == "closed"


def test_non_codebase_tagged_laundering_blocks_close(rebar_repo: Path, monkeypatch) -> None:
    """The canonical [non-codebase] spelling is guarded too, and the block names that tag."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = (
        "## Acceptance Criteria\n"
        "- [x] [non-codebase] scan scoping holds; proxy: tests/unit/test_scan_scoping.py\n"
    )
    tid = _make(rebar_repo, desc)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    err = ei.value.stderr
    assert ei.value.returncode == 1
    assert "tests/unit/test_scan_scoping.py" in err
    assert "non-codebase" in err.lower()
    assert "operator-attested" not in err.lower()
    assert _status(tid, rebar_repo) == "in_progress"


def test_non_codebase_tagged_missing_provenance_blocks_close(rebar_repo: Path, monkeypatch) -> None:
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", _never)
    desc = "## Acceptance Criteria\n- [ ] [non-codebase] the prod deploy is confirmed live\n"
    tid = _make(rebar_repo, desc)
    with pytest.raises(rebar.RebarError) as ei:
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    err = ei.value.stderr
    assert ei.value.returncode == 1
    assert "provenance" in err.lower()
    assert "non-codebase" in err.lower()
    assert "operator-attested" not in err.lower()
    assert _status(tid, rebar_repo) == "in_progress"


def test_non_codebase_item_with_provenance_still_closes(rebar_repo: Path, monkeypatch) -> None:
    """The canonical tag buys the same ADR-0043 escape hatch as the legacy spelling."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = (
        "## Acceptance Criteria\n"
        "- [x] code merged\n"
        "- [ ] [non-codebase] the prod deploy is confirmed live\n"
        f"{_PROVENANCE}\n"
    )
    tid = _make(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"


def test_unchecked_non_codebase_item_is_exempt_from_the_checkbox_precheck(
    rebar_repo: Path, monkeypatch
) -> None:
    """An unchecked [non-codebase] item with provenance does not trip the AC-box precheck."""
    _commit(rebar_repo)
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)
    desc = (
        f"## Acceptance Criteria\n- [ ] [non-codebase] the quarterly drill was run\n{_PROVENANCE}\n"
    )
    tid = _make(rebar_repo, desc)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "closed"
