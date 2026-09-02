"""No stale bare-`review` CLI guidance in canonical operational docs (ticket
2dbf-07bf-452d-4543).

`src/rebar/_cli/_registry.py` registers `review-code`, `review-plan`, and
`sign-review` -- there is no bare `review` command. `AGENTS.md` and
`docs/repo-snapshot-gates.md` previously claimed otherwise (a "deprecated,
forwards to review-plan" verb and a "retained alias" respectively). This
pins the corrected state while leaving the accurate historical records
(the ADR mapping and the release-notes/output-schemas removed-surface
entries) untouched.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Operational-guidance pages that must carry no bare-`review` claim.
_OPERATIONAL_DOCS = (
    "AGENTS.md",
    "docs/repo-snapshot-gates.md",
    "docs/exit-codes.md",
)

# Historical/removal-record pages allowed to keep a bare `review` mention.
_HISTORICAL_DOCS = (
    "docs/adr/0005-snapshot-cache-architecture.md",
    "docs/release-notes.md",
    "docs/output-schemas.md",
)

_BARE_REVIEW_RE = re.compile(r"rebar review($|[^-])")


# ─────────────────────────── HAPPY PATH ──────────────────────────────────────


def test_registry_has_no_bare_review_route():
    """The live CLI registry has no bare `review` command -- only the hyphenated
    review-code/review-plan/sign-review forms."""
    registry = (REPO_ROOT / "src" / "rebar" / "_cli" / "_registry.py").read_text(encoding="utf-8")
    assert '"review":' not in registry
    assert '"review-code"' in registry
    assert '"review-plan"' in registry


@pytest.mark.parametrize("relpath", _OPERATIONAL_DOCS)
def test_operational_doc_has_no_bare_review_claim(relpath: str):
    """AGENTS.md, repo-snapshot-gates.md, and exit-codes.md carry no claim that bare
    `review` is an available or forwarding CLI command."""
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert not _BARE_REVIEW_RE.search(text), (
        f"{relpath} still references bare `rebar review` as a live command"
    )
    assert "retained as an alias" not in text
    assert "deprecated and now forwards" not in text


def test_repo_snapshot_gates_cli_forms_list_only_live_commands():
    """The CLI-forms sentence in repo-snapshot-gates.md lists only the four live
    commands, not the retired bare `review`."""
    text = (REPO_ROOT / "docs" / "repo-snapshot-gates.md").read_text(encoding="utf-8")
    assert "`rebar review-plan`, `verify-completion`, `review-code`, and `scan-spec`" in text


# ─────────────────────────── EDGE CASES (HELD OUT) ────────────────────────────


def test_review_code_review_plan_sign_review_rows_remain_in_exit_codes():
    """Deleting nothing that's live: the exit-codes rows for the real review commands
    are still present and accurate."""
    text = (REPO_ROOT / "docs" / "exit-codes.md").read_text(encoding="utf-8")
    for row in ("`review-code`", "`review-plan`", "`sign-review`"):
        assert row in text


@pytest.mark.parametrize("relpath", _HISTORICAL_DOCS)
def test_historical_removal_records_are_preserved(relpath: str):
    """The ADR mapping and removed-surface records are read-only evidence and must
    keep naming the removed bare `review` verb -- they are NOT rewritten."""
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")
    assert "review" in text.lower()
