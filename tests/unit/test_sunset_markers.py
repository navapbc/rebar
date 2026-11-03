"""The close-time half of the sunset-marker two-way door (d68f-4771).

The ratchet's grammar half makes an admission NAME the ticket that will retire it. On its
own that is still a one-way door: every one of the six deprecated-quad citations this change
migrated pointed at a ticket that was already CLOSED, so the admission had outlived its own
justification with nothing to notice. This half closes the door from the other side.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._commands._seam import CommandError
from rebar._commands.sunset_markers import (
    check_no_open_sunset_marker,
    marker_reason,
    open_sunset_sites,
    sunset_ticket,
)

pytestmark = pytest.mark.unit

TICKET = "d68f-4771-ebc1-4ee4"


def _repo(tmp_path: Path, text: str, *, track: bool = True) -> str:
    """A git repo holding one file, tracked unless asked otherwise."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    target = tmp_path / "mod.py"
    target.write_text(f"x = 1\n{text}\ny = 2\n", encoding="utf-8")
    if track:
        subprocess.run(["git", "-C", str(tmp_path), "add", "mod.py"], check=True)
    return str(tmp_path)


def _marker(ticket: str) -> str:
    return f"# mechanism-ok: lock probe.lock — {ticket} bounded by the drain it replaces"


def test_the_close_is_refused_while_a_marker_names_the_ticket(tmp_path):
    """AC2. The admission's own retiring ticket cannot quietly go away beneath it."""
    root = _repo(tmp_path, _marker(TICKET))
    with pytest.raises(CommandError) as excinfo:
        check_no_open_sunset_marker(TICKET, TICKET, root, root)
    assert "cannot close" in str(excinfo.value)


def test_the_refusal_names_the_file_and_line_so_it_is_actionable(tmp_path):
    """A refusal that does not say WHERE forces a tree-wide hunt for the marker."""
    root = _repo(tmp_path, _marker(TICKET))
    with pytest.raises(CommandError) as excinfo:
        check_no_open_sunset_marker(TICKET, TICKET, root, root)
    assert "mod.py:2" in str(excinfo.value), str(excinfo.value)


def test_the_close_succeeds_once_the_marker_is_removed(tmp_path):
    """The refusal is a gate, not a trap: the remedy it names actually clears it."""
    root = _repo(tmp_path, _marker(TICKET))
    (tmp_path / "mod.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", root, "add", "mod.py"], check=True)
    check_no_open_sunset_marker(TICKET, TICKET, root, root)


def test_a_ticket_named_by_no_marker_is_unaffected(tmp_path):
    """The overwhelming majority of closes must not pay for this check."""
    root = _repo(tmp_path, _marker("933f-e7d8-696a-4fbd"))
    check_no_open_sunset_marker(TICKET, TICKET, root, root)


def test_a_two_quad_marker_blocks_the_four_quad_close(tmp_path):
    """AGENTS.md lists the two-quad short id as a preferred citation form, so a marker
    written that way has to match the canonical id the close resolves to."""
    root = _repo(tmp_path, _marker("d68f-4771"))
    with pytest.raises(CommandError):
        check_no_open_sunset_marker(TICKET, TICKET, root, root)


def test_a_bare_single_quad_marker_blocks_nothing(tmp_path):
    """The deprecated 4-digit form names nothing, so it cannot establish a sunset. Treating
    it as a match would block every ticket sharing that quad prefix."""
    root = _repo(tmp_path, _marker("d68f"))
    check_no_open_sunset_marker(TICKET, TICKET, root, root)


def test_an_untracked_marker_does_not_block_a_close(tmp_path):
    """A scratch file in someone's worktree is not a statement about the project."""
    root = _repo(tmp_path, _marker(TICKET), track=False)
    check_no_open_sunset_marker(TICKET, TICKET, root, root)


def test_the_scan_needs_no_ci_environment(tmp_path, monkeypatch):
    """AC3. The refusal is operation-linked, not CI-linked: it has to hold in a bare
    checkout with no CI provider at all, which is the portability constraint rebar is
    held to (`project.portability`)."""
    for name in ("CI", "GITHUB_ACTIONS", "GITHUB_WORKFLOW", "GITHUB_RUN_ID"):
        monkeypatch.delenv(name, raising=False)
    root = _repo(tmp_path, _marker(TICKET))
    assert open_sunset_sites(TICKET, root, root) == [("mod.py", 2)]


def test_a_marker_with_no_sunset_ticket_blocks_nothing(tmp_path):
    """Markers predating the grammar carry free prose. They are the ratchet's problem, and
    must not make every close in the tree fail."""
    root = _repo(tmp_path, "# mechanism-ok: lock probe.lock — remove after the migration")
    assert sunset_ticket(marker_reason("# mechanism-ok: lock probe.lock — remove after")) == ""
    check_no_open_sunset_marker(TICKET, TICKET, root, root)


def test_every_site_is_reported_not_just_the_first(tmp_path):
    """One marker per close would turn a multi-site retirement into a guessing game."""
    root = _repo(tmp_path, f"{_marker(TICKET)}\n{_marker(TICKET)}")
    assert len(open_sunset_sites(TICKET, root, root)) == 2


def _tracker(tmp_path: Path, canonical: str, alias: str) -> str:
    """A minimal ticket store whose CREATE event carries ``alias``."""
    import json

    tracker = tmp_path / "tracker"
    (tracker / canonical).mkdir(parents=True)
    (tracker / canonical / "0001-CREATE.json").write_text(
        json.dumps({"data": {"alias": alias}}), encoding="utf-8"
    )
    return str(tracker)


def test_an_alias_marker_blocks_the_close_of_the_ticket_it_names(tmp_path):
    """The sharp edge, and the only branch that opens the store.

    AGENTS.md makes the alias the PREFERRED citation form, and one in-tree marker already
    uses it. Shape comparison alone cannot connect `metaphoric-late-creature` to the
    four-quad id a close resolves to, so without this resolution that marker would be the
    one admission the gate could never enforce.
    """
    root = _repo(tmp_path / "repo", _marker("metaphoric-late-creature"))
    tracker = _tracker(tmp_path, TICKET, "metaphoric-late-creature")
    with pytest.raises(CommandError):
        check_no_open_sunset_marker(TICKET, TICKET, root, tracker)


def test_an_alias_naming_a_different_ticket_does_not_block(tmp_path):
    """Resolution has to discriminate, not just succeed."""
    root = _repo(tmp_path / "repo", _marker("some-other-alias"))
    tracker = _tracker(tmp_path, TICKET, "metaphoric-late-creature")
    check_no_open_sunset_marker(TICKET, TICKET, root, tracker)


def test_an_alias_resolving_to_another_real_ticket_does_not_block(tmp_path):
    """The discriminating branch, exercised where resolution SUCCEEDS.

    An alias absent from the store fails to resolve, so a guard that blocked on any
    successful resolution — or that compared the raw strings — would still pass that case
    and look correct. Here the marker's alias names a real, different ticket: resolution
    returns a canonical id, and only comparing it against the closing ticket's own id
    distinguishes the two.
    """
    root = _repo(tmp_path / "repo", _marker("some-other-alias"))
    tracker = _tracker(tmp_path, TICKET, "metaphoric-late-creature")
    _tracker(tmp_path, "9f21-4c7a-b0e3-8d15", "some-other-alias")
    check_no_open_sunset_marker(TICKET, TICKET, root, tracker)


def test_a_code_root_outside_git_yields_no_markers(tmp_path):
    """An empty answer, not a swallowed failure.

    A directory with no tracked set cannot hold a tracked marker, so there is nothing to
    refuse and a close there must proceed. This is the ONE scan outcome that is legitimately
    silent, which is why it is named explicitly rather than folded into a blanket except.
    """
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    tracker = _tracker(tmp_path, TICKET, "metaphoric-late-creature")
    check_no_open_sunset_marker(TICKET, TICKET, str(plain), tracker)


def test_a_failed_scan_raises_instead_of_passing_the_close(tmp_path, monkeypatch):
    """The guard must not answer 'no markers' when it could not look.

    A scan that fails for any reason other than a missing repository leaves the marker set
    UNKNOWN. Reporting that as an empty set is indistinguishable from a clean tree, so a
    close that should have been refused would go through and the admission would survive
    its own sunset ticket — the exact outcome this guard exists to prevent.
    """
    root = _repo(tmp_path / "repo", _marker(TICKET))
    tracker = _tracker(tmp_path, TICKET, "metaphoric-late-creature")

    def _boom(args, *_rest, **_kwargs):
        # The repository IS there — only the grep fails — so the guard cannot excuse itself
        # with the missing-repository case.
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, ".git\n", "")
        return subprocess.CompletedProcess(args, 128, "", "fatal: unable to read index")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="cannot scan"):
        check_no_open_sunset_marker(TICKET, TICKET, root, tracker)


def test_a_code_root_that_does_not_exist_yields_no_markers(tmp_path):
    """A path that is not there holds no tracked set, exactly as a non-repository does.

    ``git grep`` reports a missing directory and a directory outside any repository with the
    SAME exit status and different prose, so a guard that told them apart by matching the
    message would refuse every close whose code root had not been created — including the
    callers that pass a placeholder root.
    """
    tracker = _tracker(tmp_path, TICKET, "metaphoric-late-creature")
    check_no_open_sunset_marker(TICKET, TICKET, str(tmp_path / "absent"), tracker)


def test_an_unrunnable_git_raises_instead_of_passing_the_close(tmp_path, monkeypatch):
    """Same reasoning for the process that never starts as for the one that fails."""
    root = _repo(tmp_path / "repo", _marker(TICKET))
    tracker = _tracker(tmp_path, TICKET, "metaphoric-late-creature")

    def _no_git(*_args, **_kwargs):
        raise OSError("git: command not found")

    monkeypatch.setattr(subprocess, "run", _no_git)
    with pytest.raises(RuntimeError, match="cannot scan"):
        check_no_open_sunset_marker(TICKET, TICKET, root, tracker)
