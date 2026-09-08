"""Subtree-aware referencing-commit precondition (bug ferric-jet-scorpion / 1edf).

The completion-gate's deterministic referencing-commit precondition
(``rebar._commands.transition_close._completion_precheck`` /
``rebar._commands.close_precheck._referencing_commits``) must credit a parent's ENTIRE
descendant subtree: a
ticket that records ``file_impact`` closes when a ``rebar-ticket:`` trailer references it
OR any of its descendants. A parent's code is delivered by its children's commits, so an
epic/story must not be forced into ``--force`` (unsigned) merely because the
referencing commits carry the child ids.

This is safe: the open-children guard runs first, so a parent only reaches this
precondition once every child is closed — and each child already passed this exact
referencing-commit check at its own close.

Happy path here; the transitive-grandchild, still-blocks-without-subtree-commit, and
leaf no-regression cases are validated by the held-out companion suite.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
import rebar.llm

_DESC = "Body.\n\n## Acceptance Criteria\n- [x] done\n\n## Context\nc\n"


def _enable(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def PASS(ticket_id, **kw):
    return {"verdict": "PASS", "findings": [], "runner": "fake", "model": "m"}


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _commit_ref(repo: Path, ref: str) -> None:
    """Empty commit whose message carries a ``rebar-ticket: <ref>`` trailer."""
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", f"work\n\nrebar-ticket: {ref}"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _mk(repo: Path, ttype: str, *, parent: str | None = None, file_impact: bool = False) -> str:
    tid = rebar.create_ticket(
        ttype, f"{ttype} t", description=_DESC, parent=parent, repo_root=str(repo)
    )
    if file_impact:
        rebar.set_file_impact(tid, [{"path": "src/x.py", "reason": "touched"}], repo_root=str(repo))
    return tid


def _close(tid: str, repo: Path) -> None:
    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))


def test_parent_closes_when_a_child_commit_references_the_subtree(
    rebar_repo: Path, monkeypatch
) -> None:
    """An epic that records file_impact but whose only referencing commit carries the CHILD's
    id closes: the precondition credits the descendant subtree, the verifier runs, and the
    epic closes signed — no --force needed."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    child = _mk(rebar_repo, "task", parent=epic)  # no file_impact -> child closes freely

    # Move the leaf into progress (cascades the open parent into progress too), then commit
    # a trailer referencing ONLY the child, and close the child.
    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, child)
    _close(child, rebar_repo)
    assert _status(child, rebar_repo) == "closed"

    # The epic records file_impact and NO commit references the epic's own id — only the
    # child's. With the subtree fix the precondition passes and the epic closes signed.
    _close(epic, rebar_repo)
    assert _status(epic, rebar_repo) == "closed"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_parent_alias_close_credits_descendant_commit(rebar_repo: Path, monkeypatch) -> None:
    """Closing a parent by alias must credit the same descendant subtree as closing by id."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    child = _mk(rebar_repo, "task", parent=epic)
    epic_alias = str(rebar.show_ticket(epic, repo_root=str(rebar_repo))["alias"])

    rebar.transition(child, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, child)
    _close(child, rebar_repo)

    _close(epic_alias, rebar_repo)
    assert _status(epic, rebar_repo) == "closed"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_parent_close_credits_duplicate_child_replacement_commit(
    rebar_repo: Path, monkeypatch
) -> None:
    """A duplicate child's replacement is where the landed work lives, so parent close
    evidence must credit the replacement's referencing commit."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    duplicate_child = _mk(rebar_repo, "task", parent=epic, file_impact=True)
    replacement = _mk(rebar_repo, "task", file_impact=True)
    rebar.link(duplicate_child, replacement, "duplicates", repo_root=str(rebar_repo))

    rebar.transition(replacement, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, replacement)
    _close(replacement, rebar_repo)

    rebar.transition(duplicate_child, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.transition(
        duplicate_child,
        "in_progress",
        "closed",
        close_class="duplicate",
        repo_root=str(rebar_repo),
    )

    _close(epic, rebar_repo)
    assert _status(epic, rebar_repo) == "closed"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_parent_close_credits_superseded_child_replacement_commit(
    rebar_repo: Path, monkeypatch
) -> None:
    """A superseded child's replacement is also valid landed-work evidence."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    superseded_child = _mk(rebar_repo, "task", parent=epic, file_impact=True)
    replacement = _mk(rebar_repo, "task", file_impact=True)
    rebar.link(replacement, superseded_child, "supersedes", repo_root=str(rebar_repo))

    rebar.transition(replacement, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, replacement)
    _close(replacement, rebar_repo)

    rebar.transition(superseded_child, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.transition(
        superseded_child,
        "in_progress",
        "closed",
        close_class="superseded",
        repo_root=str(rebar_repo),
    )

    _close(epic, rebar_repo)
    assert _status(epic, rebar_repo) == "closed"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_parent_close_credits_replacement_descendant_commit(rebar_repo: Path, monkeypatch) -> None:
    """A replacement's descendants are part of the replacement evidence scope."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    duplicate_child = _mk(rebar_repo, "task", parent=epic, file_impact=True)
    replacement_epic = _mk(rebar_repo, "epic")
    replacement_child = _mk(rebar_repo, "task", parent=replacement_epic)
    rebar.link(duplicate_child, replacement_epic, "duplicates", repo_root=str(rebar_repo))

    rebar.transition(replacement_child, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, replacement_child)
    _close(replacement_child, rebar_repo)
    _close(replacement_epic, rebar_repo)

    rebar.transition(duplicate_child, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.transition(
        duplicate_child,
        "in_progress",
        "closed",
        close_class="duplicate",
        repo_root=str(rebar_repo),
    )

    _close(epic, rebar_repo)
    assert _status(epic, rebar_repo) == "closed"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_parent_close_follows_chained_disposition_replacements(
    rebar_repo: Path, monkeypatch
) -> None:
    """Replacement expansion follows a replacement that is itself a disposition."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    duplicate_child = _mk(rebar_repo, "task", parent=epic, file_impact=True)
    first_replacement = _mk(rebar_repo, "task", file_impact=True)
    final_replacement = _mk(rebar_repo, "task", file_impact=True)
    rebar.link(duplicate_child, first_replacement, "duplicates", repo_root=str(rebar_repo))
    rebar.link(first_replacement, final_replacement, "duplicates", repo_root=str(rebar_repo))

    rebar.transition(final_replacement, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, final_replacement)
    _close(final_replacement, rebar_repo)

    rebar.transition(first_replacement, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.transition(
        first_replacement,
        "in_progress",
        "closed",
        close_class="duplicate",
        repo_root=str(rebar_repo),
    )
    rebar.transition(duplicate_child, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.transition(
        duplicate_child,
        "in_progress",
        "closed",
        close_class="duplicate",
        repo_root=str(rebar_repo),
    )

    _close(epic, rebar_repo)
    assert _status(epic, rebar_repo) == "closed"
    assert rebar.verify_signature(epic, repo_root=str(rebar_repo))["verdict"] == "certified"


def test_duplicate_replacement_without_a_commit_still_blocks(rebar_repo: Path, monkeypatch) -> None:
    """Replacement expansion must not erase the landed-work precondition."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    duplicate_child = _mk(rebar_repo, "task", parent=epic, file_impact=True)
    replacement = _mk(rebar_repo, "task", file_impact=True)
    rebar.link(duplicate_child, replacement, "duplicates", repo_root=str(rebar_repo))

    rebar.transition(duplicate_child, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.transition(
        duplicate_child,
        "in_progress",
        "closed",
        close_class="duplicate",
        repo_root=str(rebar_repo),
    )

    with pytest.raises(rebar.RebarError) as ei:
        _close(epic, rebar_repo)
    assert "file_impact" in ei.value.stderr
    assert "commit" in ei.value.stderr.lower()
    assert _status(epic, rebar_repo) == "in_progress"


def test_non_disposition_bug_close_does_not_credit_replacement(
    rebar_repo: Path, monkeypatch
) -> None:
    """Resolution classes like preexisting are not replacement dispositions."""
    _enable(rebar_repo)
    monkeypatch.setattr(rebar.llm, "verify_completion", PASS)

    epic = _mk(rebar_repo, "epic", file_impact=True)
    preexisting_child = _mk(rebar_repo, "bug", parent=epic)
    replacement = _mk(rebar_repo, "task", file_impact=True)
    rebar.link(preexisting_child, replacement, "duplicates", repo_root=str(rebar_repo))

    rebar.transition(replacement, "open", "in_progress", repo_root=str(rebar_repo))
    _commit_ref(rebar_repo, replacement)
    _close(replacement, rebar_repo)

    rebar.transition(preexisting_child, "open", "in_progress", repo_root=str(rebar_repo))
    rebar.transition(
        preexisting_child,
        "in_progress",
        "closed",
        close_class="preexisting",
        repo_root=str(rebar_repo),
    )

    with pytest.raises(rebar.RebarError) as ei:
        _close(epic, rebar_repo)
    assert "file_impact" in ei.value.stderr
    assert "commit" in ei.value.stderr.lower()
    assert _status(epic, rebar_repo) == "in_progress"


def test_pinned_error_replacement_is_not_live() -> None:
    """Pinned liveness matches materialized liveness: errored tickets are not live."""
    from rebar._commands.close_scope import replacement_target_for_closed_disposition

    class View:
        def resolve(self, ref):
            return {"duplicate": "duplicate", "replacement": "replacement"}.get(ref)

        def show_ticket(self, ref, *, include_inbound=False):
            if ref == "duplicate":
                return {
                    "status": "closed",
                    "close_class": "duplicate",
                    "deps": [{"relation": "duplicates", "target_id": "replacement"}],
                    "inbound_deps": [],
                }
            if ref == "replacement":
                return {"status": "open", "error": "unreadable reduced state"}
            raise AssertionError(ref)

    assert replacement_target_for_closed_disposition("duplicate", "", ticket_view=View()) is None


def test_pinned_duplicate_replacement_expands_scope() -> None:
    """Pinned close prechecks credit a closed duplicate's live replacement."""
    from rebar._commands.close_scope import expand_with_disposition_replacements

    class View:
        def resolve(self, ref):
            return {
                "duplicate": "duplicate",
                "replacement": "replacement",
                "replacement-child": "replacement-child",
            }.get(ref)

        def show_ticket(self, ref, *, include_inbound=False):
            if ref == "duplicate":
                return {
                    "status": "closed",
                    "close_class": "duplicate",
                    "deps": [{"relation": "duplicates", "target_id": "replacement"}],
                    "inbound_deps": [],
                }
            if ref in {"replacement", "replacement-child"}:
                return {"status": "open"}
            raise AssertionError(ref)

        def transitive_descendant_ids(self, ref):
            return ["replacement-child"] if ref == "replacement" else []

    assert expand_with_disposition_replacements({"duplicate"}, "", ticket_view=View()) == {
        "duplicate",
        "replacement",
        "replacement-child",
    }


def test_pinned_archived_replacement_does_not_expand_scope() -> None:
    """Pinned close prechecks do not credit archived replacements."""
    from rebar._commands.close_scope import expand_with_disposition_replacements

    class View:
        def resolve(self, ref):
            return {"duplicate": "duplicate", "replacement": "replacement"}.get(ref)

        def show_ticket(self, ref, *, include_inbound=False):
            if ref == "duplicate":
                return {
                    "status": "closed",
                    "close_class": "duplicate",
                    "deps": [{"relation": "duplicates", "target_id": "replacement"}],
                    "inbound_deps": [],
                }
            if ref == "replacement":
                return {"status": "archived", "archived": True}
            raise AssertionError(ref)

        def transitive_descendant_ids(self, ref):
            raise AssertionError(ref)

    assert expand_with_disposition_replacements({"duplicate"}, "", ticket_view=View()) == {
        "duplicate"
    }
