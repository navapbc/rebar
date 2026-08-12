"""The batched ticket-scoped resolver must attribute EXACTLY as the per-event one
(bug 7084 / remediation R1).

Compaction used to resolve each signed event's introducing commit with its own
``git log --diff-filter=A --full-history`` — 6.78s per event on a 71k-commit tickets
branch, 47.5s of a measured 48.1s ``compact-on-close``, all of it inside the store write
lock. R1 replaces that with ONE directory-scoped walk.

This feeds ``rebar verify-authorship``, so a wrong or missing commit attribution is an
attestation-chain correctness failure, not a performance one. These tests therefore assert
EQUIVALENCE against the old per-event resolver over a real git history — including the
case ``--full-history`` exists for: an event whose introducing commit is OFF the
first-parent chain, where default history simplification would answer differently.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from rebar.attest import authorship

pytestmark = pytest.mark.unit

TICKET = "aaaa-bbbb-cccc-dddd"


class _GitFailed(subprocess.CalledProcessError):
    """``CalledProcessError`` that RENDERS the captured output.

    The base class stores ``stdout``/``stderr`` but its ``__str__`` prints only the argv and
    the exit status, so a helper running with ``capture_output=True`` captures git's own
    diagnosis and then discards it at the point it matters. A fixture-setup failure then
    reaches CI as a bare "returned non-zero exit status 1" — which is why the merge failure in
    bug warmthless-dermal-oropendola could not be attributed: ``git merge`` exits 1 for a
    content conflict, a failing hook, AND an unresolvable argument alike. Subclassing keeps the
    exception TYPE (existing ``except subprocess.CalledProcessError`` clauses still catch it)
    and changes only what is rendered.
    """

    def __str__(self) -> str:
        return (
            f"{super().__str__()}\n"
            f"argv:   {self.cmd}\n"
            f"stdout:\n{self.output}\n"
            f"stderr:\n{self.stderr}"
        )


def _git(repo: Path, *args: str) -> str:
    argv = ["git", "-C", str(repo), *args]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        raise _GitFailed(proc.returncode, argv, output=proc.stdout, stderr=proc.stderr)
    return proc.stdout.strip()


def _event(repo: Path, position: str, event_type: str, *, body: str = "x") -> str:
    """Write + commit one event file, returning its relative path."""
    ticket_dir = repo / TICKET
    ticket_dir.mkdir(exist_ok=True)
    rel = f"{TICKET}/{position}-{event_type}.json"
    (repo / rel).write_text(
        json.dumps(
            {
                "uuid": position.split("-", 1)[1],
                "timestamp": position.split("-", 1)[0],
                "event_type": event_type,
                "body": body,
            }
        )
    )
    _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", f"ticket: {event_type} {TICKET}")
    return rel


@pytest.fixture
def tracker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A tracker whose history is NON-TRIVIAL in the way a real store's is: a mainline,
    a divergent branch that adds events independently (the other clone / other agent), a
    merge, and — critically — ONE event added on BOTH sides before the merge.

    That last one is the off-first-parent case. The merge is TREESAME to both parents for
    that path, so default history simplification follows only the first parent and reports
    the MAINLINE add; ``--full-history`` sees the side-branch add too and, being older,
    that is the real introducing commit. Any resolver that drops ``--full-history``
    answers differently here — which is exactly why the flag is load bearing."""
    repo = tmp_path / "tracker"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "tickets")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")

    _event(repo, "1000-aaaa1111", "CREATE")
    _event(repo, "1001-aaaa2222", "COMMENT")
    base = _git(repo, "rev-parse", "HEAD")

    # Divergent side branch (another clone): two adds of its own, plus the shared event.
    _git(repo, "checkout", "-q", "-b", "side")
    _event(repo, "1002-bbbb1111", "COMMENT")
    _event(repo, "1500-shared01", "STATUS", body="shared")  # <- also created on mainline
    _event(repo, "1003-bbbb2222", "EDIT")

    _git(repo, "checkout", "-q", "tickets")
    _git(repo, "reset", "-q", "--hard", base)
    _event(repo, "1004-cccc1111", "COMMENT")
    _event(repo, "1500-shared01", "STATUS", body="shared")  # identical path + content
    _event(repo, "1005-cccc2222", "COMMENT")
    _git(repo, "merge", "-q", "--no-edit", "side")
    _event(repo, "1006-dddd1111", "COMMENT")

    from rebar._commands import _seam

    monkeypatch.setattr(_seam, "tracker_dir", lambda repo_root=None: repo)
    return repo


def _positions(repo: Path) -> list[str]:
    return sorted(p.name[: -len(".json")].rsplit("-", 1)[0] for p in (repo / TICKET).glob("*.json"))


def test_batched_map_matches_the_per_event_resolver_for_every_event(tracker: Path) -> None:
    """The equivalence property: identical commit attribution for every event of a
    realistic ticket."""
    ticket_dir = str(tracker / TICKET)
    batched = authorship.build_ticket_position_commit_map(ticket_dir)

    positions = _positions(tracker)
    assert len(positions) == 8  # 2 base + 2 side + 2 mainline + 1 shared + 1 post-merge

    for position in positions:
        expected = authorship.resolve_event_commit(position, ticket_dir)
        assert expected is not None, position
        assert batched.get(position) == expected, position


def test_the_off_first_parent_add_is_resolved_to_the_older_side_branch_commit(
    tracker: Path,
) -> None:
    """The case ``--full-history`` exists for, asserted explicitly rather than left to the
    sweep above: the shared event's introducing commit is the SIDE-BRANCH add, which is
    off the first-parent chain, and it is what BOTH resolvers return."""
    ticket_dir = str(tracker / TICKET)
    position = "1500-shared01"

    batched = authorship.build_ticket_position_commit_map(ticket_dir)[position]
    per_event = authorship.resolve_event_commit(position, ticket_dir)
    assert batched == per_event

    # It is genuinely off the first-parent chain: the simplified history answers with a
    # DIFFERENT (newer, mainline) commit, so this is not a case both flags agree on.
    simplified = subprocess.run(
        [
            "git",
            "-C",
            str(tracker),
            "log",
            "--diff-filter=A",
            "--no-renames",
            "--format=%H",
            "--",
            f"{TICKET}/{position}-*.json",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert simplified, "expected the simplified query to find something"
    assert simplified[-1] != batched, "fixture no longer exercises the --full-history case"

    # And the resolved commit really is the side branch's add.
    subject = _git(tracker, "log", "-1", "--format=%H", batched)
    assert subject == batched
    branches = _git(tracker, "branch", "--contains", batched, "--all")
    assert "side" in branches


def test_ledger_attribution_is_unchanged_by_the_batching(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end at the call site R1 changes: the authorship ledger compaction builds
    carries byte-identical ``commit_sha`` values to the OLD per-event loop."""
    from rebar._commands import compact

    ticket_dir = tracker / TICKET
    paths = sorted(str(p) for p in ticket_dir.glob("*.json"))

    # Sign every event so each one produces a ledger entry.
    for path in paths:
        data = json.loads(Path(path).read_text())
        data["author_sig"] = "not-a-real-envelope"
        data["author_id"] = "id-1"
        Path(path).write_text(json.dumps(data))

    ledger = compact._build_authorship_ledger(paths, None)
    assert len(ledger) == len(paths)

    # The OLD attribution, computed the way the pre-R1 loop did it.
    for entry in ledger:
        position = entry["position"]["position"]
        expected = authorship.resolve_event_commit(position, str(ticket_dir))
        if expected is None:
            expected = authorship.resolve_position_commit(position, str(tracker))
        assert entry["position"]["commit_sha"] == expected, position
        assert expected is not None


def test_batching_costs_one_git_walk_not_one_per_event(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The point of R1: the number of full-history walks stops scaling with the number of
    signed events. Counted rather than timed, so it holds on any machine."""
    from rebar._commands import compact

    ticket_dir = tracker / TICKET
    paths = sorted(str(p) for p in ticket_dir.glob("*.json"))
    for path in paths:
        data = json.loads(Path(path).read_text())
        data["author_sig"] = "not-a-real-envelope"
        data["author_id"] = "id-1"
        Path(path).write_text(json.dumps(data))

    per_event_calls: list[str] = []
    real_resolve = authorship.resolve_event_commit
    monkeypatch.setattr(
        authorship,
        "resolve_event_commit",
        lambda position, td, **kw: (
            per_event_calls.append(position),
            real_resolve(position, td, **kw),
        )[1],
    )

    compact._build_authorship_ledger(paths, None)

    assert len(paths) >= 8
    assert per_event_calls == [], "every position should have been served by the batched map"


def test_a_git_failure_falls_back_to_the_per_event_resolver(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: an empty map (any git failure) must not null out attribution — the
    ledger falls back to the resolver it used before R1 and the shas are unchanged."""
    from rebar._commands import compact

    ticket_dir = tracker / TICKET
    paths = sorted(str(p) for p in ticket_dir.glob("*.json"))
    for path in paths:
        data = json.loads(Path(path).read_text())
        data["author_sig"] = "not-a-real-envelope"
        data["author_id"] = "id-1"
        Path(path).write_text(json.dumps(data))

    expected = {
        e["position"]["position"]: e["position"]["commit_sha"]
        for e in compact._build_authorship_ledger(paths, None)
    }

    monkeypatch.setattr(authorship, "build_ticket_position_commit_map", lambda *a, **kw: {})
    fallback = {
        e["position"]["position"]: e["position"]["commit_sha"]
        for e in compact._build_authorship_ledger(paths, None)
    }

    assert fallback == expected
    assert all(sha is not None for sha in fallback.values())


def test_map_is_empty_and_never_raises_when_git_fails(tmp_path: Path) -> None:
    assert authorship.build_ticket_position_commit_map("") == {}
    assert authorship.build_ticket_position_commit_map(str(tmp_path / "nope")) == {}


def test_git_failure_surfaces_git_own_diagnostics(tmp_path: Path) -> None:
    """A failing ``_git`` names the argv, the exit code, and git's OWN stdout/stderr.

    The fixture helper captures git's output, and ``CalledProcessError.__str__`` does not
    render it — so a fixture-setup failure reached CI as a bare "returned non-zero exit
    status 1" with git's message captured and discarded. That is why the merge failure in
    bug warmthless-dermal-oropendola could not be attributed: ``git merge`` exits 1 for a
    content conflict, a failing hook, AND an unresolvable argument alike, and the record
    kept nothing that distinguishes them.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "tickets")

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _git(repo, "merge", "-q", "--no-edit", "no-such-branch")

    rendered = str(excinfo.value)
    assert "not something we can merge" in rendered, (
        f"git's own stderr must be surfaced, got:\n{rendered}"
    )
    assert "merge" in rendered, "the failing argv must be named"
    assert "no-such-branch" in rendered, "the failing argv must be named in full"
    assert "1" in rendered, "the exit code must be named"
