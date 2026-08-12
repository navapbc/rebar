"""Primary contract for best-effort blame failures (ticket 42fa)."""

from __future__ import annotations

from rebar.metrics import blame


def test_blame_file_commits_returns_none_when_git_cannot_blame(monkeypatch) -> None:
    """A failed blame is unavailable data, not a successfully empty file."""
    monkeypatch.setattr(blame, "_git", lambda *args: None)

    assert blame._blame_file_commits("/repo", "fix~1", "missing.py") is None


def test_blame_file_commits_distinguishes_failure_from_successful_empty(monkeypatch) -> None:
    monkeypatch.setattr(blame, "_git", lambda *args: None)
    assert blame._blame_file_commits("/repo", "fix~1", "missing.py") is None

    monkeypatch.setattr(blame, "_git", lambda *args: "")
    assert blame._blame_file_commits("/repo", "fix~1", "empty.py") == []


def test_partial_failure_aborts_but_successful_empty_does_not(monkeypatch) -> None:
    monkeypatch.setattr(blame, "_find_fixing_commit", lambda *args: "fix-sha")
    monkeypatch.setattr(
        blame.field_reads,
        "file_impact",
        lambda *args: [
            {"path": "known.py", "reason": "changed"},
            {"path": "other.py", "reason": "changed"},
        ],
    )
    monkeypatch.setattr(
        blame, "resolve_ticket_id", lambda ticket_id, tracker, quiet=False: ticket_id
    )
    monkeypatch.setattr(blame, "_commit_ticket", lambda *args: "culprit-ticket")

    results: dict[str, list[str] | None] = {
        "known.py": ["introducing-sha", "introducing-sha"],
        "other.py": None,
    }
    monkeypatch.setattr(blame, "_blame_file_commits", lambda root, ref, path: results[path])

    assert blame.derive_caused_by("bug-ticket", "/repo", "/tracker") is None

    # A successful empty file contributes zero lines but does not invalidate the
    # complete result from the remaining paths.
    results["other.py"] = []
    assert blame.derive_caused_by("bug-ticket", "/repo", "/tracker") == "culprit-ticket"

    # Existing all-success behavior remains unchanged as another negative control.
    results["other.py"] = ["introducing-sha"]
    assert blame.derive_caused_by("bug-ticket", "/repo", "/tracker") == "culprit-ticket"
