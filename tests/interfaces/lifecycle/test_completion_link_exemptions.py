"""Which link shapes exempt a non-completion bug close from the completion verifier.

Three outcomes, not two. A shape either SKIPS the verifier (a disposition against a usable
replacement), RUNS it (an ordinary close, which then fails on its own merits), or is BLOCKED
BEFORE it (bug c8fd: a `--class duplicate` close with no usable replacement, which the verifier
could only ever fail while offering remediation the operator cannot follow). The third outcome
used to be folded into the second, and that fallthrough is the defect c8fd fixes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_completion_gate import FAIL, _enable, _make, _status

import rebar
import rebar.llm

SKIP = "skip"  # disposition: exempt, closes without the verifier
VERIFY = "verify"  # ordinary close: the verifier runs and decides
BLOCK_EARLY = "block_early"  # c8fd: refused before the verifier, with the link remedy named
BLOCK_REASON = "block_reason"  # d54b: refused before the verifier, naming --reason OR a link


@pytest.mark.parametrize(
    ("shape", "close_class", "expect"),
    [
        ("replacement_supersedes_bug", "not_a_bug", SKIP),
        ("bug_duplicates_canonical", "duplicate", SKIP),
        ("closed_duplicate_target", "duplicate", SKIP),
        ("bug_supersedes_replacement", "not_a_bug", BLOCK_REASON),
        ("duplicate_link_wrong_class", "regression", VERIFY),
        ("no_relation", "not_a_bug", BLOCK_REASON),
        ("no_relation", "duplicate", BLOCK_EARLY),
        ("archived_duplicate_target", "duplicate", BLOCK_EARLY),
    ],
)
def test_completion_gate_skips_only_valid_noncompletion_link_shapes(
    rebar_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
    shape: str,
    close_class: str,
    expect: str,
) -> None:
    calls: list[str] = []

    bug = _make(rebar_repo, "bug")
    other = rebar.create_ticket("task", "replacement", repo_root=str(rebar_repo))
    if shape == "replacement_supersedes_bug":
        rebar.link(other, bug, "supersedes", repo_root=str(rebar_repo))
    elif shape == "bug_supersedes_replacement":
        rebar.link(bug, other, "supersedes", repo_root=str(rebar_repo))
    elif shape in ("bug_duplicates_canonical", "duplicate_link_wrong_class"):
        rebar.link(bug, other, "duplicates", repo_root=str(rebar_repo))
    elif shape == "archived_duplicate_target":
        rebar.link(bug, other, "duplicates", repo_root=str(rebar_repo))
        rebar.archive(other, repo_root=str(rebar_repo))
    elif shape == "closed_duplicate_target":
        # THE COMMON CASE, end to end: you are closing a duplicate of the ticket that actually
        # got fixed, so the canonical is usually ALREADY CLOSED. The gate must key on the link
        # existing, never on its target still being open. Closed here BEFORE the gate is enabled,
        # so this setup close needs no verifier of its own.
        rebar.link(bug, other, "duplicates", repo_root=str(rebar_repo))
        rebar.transition(other, "open", "closed", repo_root=str(rebar_repo))

    # Enabled only now: every shape above is pure setup, and enabling late keeps the setup close
    # in `closed_duplicate_target` out of the verifier's call log.
    _enable(rebar_repo)

    def counted_fail(ticket_id: str, **kwargs):
        calls.append(ticket_id)
        return FAIL(ticket_id, **kwargs)

    monkeypatch.setattr(rebar.llm, "verify_completion", counted_fail)

    if expect == SKIP:
        rebar.transition(
            bug,
            "in_progress",
            "closed",
            close_class=close_class,
            repo_root=str(rebar_repo),
        )
        assert calls == []
        assert _status(bug, rebar_repo) == "closed"
        return

    with pytest.raises(rebar.RebarError) as excinfo:
        rebar.transition(
            bug,
            "in_progress",
            "closed",
            close_class=close_class,
            repo_root=str(rebar_repo),
        )
    assert _status(bug, rebar_repo) == "in_progress"

    if expect == VERIFY:
        assert calls == [bug]
        return

    if expect == BLOCK_REASON:
        # d54b: a not_a_bug/escalated close with no usable replacement used to land in VERIFY,
        # where the verifier demanded proof a nonexistent defect was fixed — an unpassable gate.
        # It is now refused pre-LLM naming both doors: add --reason, or record a replacement link.
        assert calls == [], "a reason-required disposition must not reach the verifier"
        message = str(excinfo.value)
        assert "--reason" in message and "replacement" in message
        return

    # BLOCK_EARLY: refused without spending a request, and the message must carry the one
    # action that works. Before c8fd this landed in VERIFY and printed advice — finish the
    # canonical's work here, or attest a defect no longer reproduces — that could not be followed.
    assert calls == [], "a duplicate close with no usable replacement must not reach the verifier"
    message = str(excinfo.value)
    assert "duplicates" in message and "rebar link" in message
