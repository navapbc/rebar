"""Close landed-work precheck must not print unrelated ambiguity noise (bug af11).

The completion gate's deterministic referencing-commit precheck
(``rebar._commands.transition_close._referencing_commit_exists``) scans EVERY
reachable commit message and resolves every extracted candidate id through the
shared resolver. The resolver reports ambiguity to stderr — correct when the USER
supplied the ambiguous id, pure noise when the precheck is merely walking
unrelated historical commit references. Contract (ticket af11-ac5f-4e86-4d1d):

- the scan emits NO ambiguity diagnostics for unrelated historical refs;
- the gate DECISION is unchanged in both directions — a later valid full-ID
  trailer is still found (True), and absence of one still fails (False);
- explicit, user-supplied ambiguous ids (resolved outside the scan) keep their
  diagnostics.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._commands.transition_close import _referencing_commit_exists
from rebar._ids import resolve_ticket_id

TARGET = "af99-1111-2222-3333"
AMBIG_PREFIX = "2f3c"


@pytest.fixture
def scan_env(tmp_path: Path) -> tuple[str, str]:
    """A tracker whose tickets make ``2f3c`` ambiguous, plus a real git repo whose
    history carries an ambiguous-prefix subject ref NEWER than the target's
    full-ID trailer (so the scan must resolve the ambiguous candidate first)."""
    tracker = tmp_path / "tracker"
    tracker.mkdir()
    for name in (f"{AMBIG_PREFIX}-aaaa-0000-0001", f"{AMBIG_PREFIX}-bbbb-0000-0002", TARGET):
        (tracker / name).mkdir()
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("commit", "--allow-empty", "-q", "-m", f"work\n\nrebar-ticket: {TARGET}")
    git("commit", "--allow-empty", "-q", "-m", f"{AMBIG_PREFIX}: unrelated historical work")
    return str(tracker), str(repo)


def test_scan_finds_target_without_ambiguity_noise(scan_env, capsys) -> None:
    """Preconditions hold: the prefix IS ambiguous in this tracker (control below),
    yet the scan neither prints the ambiguity error nor misses the full-ID commit."""
    tracker, repo = scan_env
    assert _referencing_commit_exists({TARGET}, tracker, repo) is True
    captured = capsys.readouterr()
    assert "Ambiguous" not in captured.err
    assert "Ambiguous" not in captured.out


def test_scan_still_fails_when_no_commit_references_the_ticket(scan_env, capsys) -> None:
    """Unchanged gate decision, failing direction: an id no commit references is
    still NOT found — quieting diagnostics must not loosen the precheck."""
    tracker, repo = scan_env
    assert _referencing_commit_exists({"dddd-9999-8888-7777"}, tracker, repo) is False
    captured = capsys.readouterr()
    assert "Ambiguous" not in captured.err
    assert "Ambiguous" not in captured.out


def test_explicit_ambiguous_prefix_keeps_its_diagnostic(scan_env, capsys) -> None:
    """Contrast control: the same resolver call a user-facing command makes (no
    quiet) still reports the ambiguity — proving the fixture's prefix is genuinely
    ambiguous AND that target-id diagnostics survive the fix."""
    tracker, _repo = scan_env
    assert resolve_ticket_id(AMBIG_PREFIX, tracker) is None
    assert "Ambiguous prefix" in capsys.readouterr().err
