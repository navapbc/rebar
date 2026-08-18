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
import os
import subprocess
from collections.abc import Callable
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

    ``forensics`` (bug innovative-dandruffy-deer) additionally carries an object-store
    snapshot taken the moment the command failed — see ``_forensics``.
    """

    forensics: str = ""

    def __str__(self) -> str:
        return (
            f"{super().__str__()}\n"
            f"argv:   {self.cmd}\n"
            f"stdout:\n{self.output}\n"
            f"stderr:\n{self.stderr}\n"
            f"object-store forensics:\n{self.forensics}"
        )


def _forensics(repo: Path) -> str:
    """Snapshot the object store at the moment a git command fails.

    Bug innovative-dandruffy-deer: the fixture merge failed twice on CI with ``invalid
    object ... git write-tree failed to write a tree`` — a loose blob committed two
    subprocess calls earlier was GONE (injection proved only file ABSENCE reproduces the
    signature; corruption does not). git's stderr names the missing object but cannot say
    why it is missing, so capture what the next occurrence needs: ``fsck`` (every missing
    object), ``count-objects -v`` (loose/packed census), the ``.git/objects`` listing with
    sizes and mtimes (what survived, and when it was written), and the ``GIT_*``
    environment (rules object-store redirection in or out). Best-effort by design — a
    forensics failure must never mask the original error.
    """

    def run(*args: str) -> str:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
        return f"{proc.stdout}{proc.stderr}".strip()

    def listing(objects: Path) -> str:
        lines = []
        for path in sorted(objects.rglob("*")):
            try:
                if path.is_file():
                    stat = path.stat()
                    lines.append(
                        f"{path.relative_to(objects)}  size={stat.st_size}"
                        f"  mtime={stat.st_mtime:.6f}"
                    )
            except OSError as exc:  # raced away mid-listing: itself evidence, record it
                lines.append(f"{path.relative_to(objects)}  <stat failed: {exc}>")
        return "\n".join(lines) or "(empty)"

    try:
        objects = repo / ".git" / "objects"
        env = "\n".join(f"{k}={v}" for k, v in sorted(os.environ.items()) if k.startswith("GIT_"))
        return (
            f"fsck --full:\n{run('fsck', '--full')}\n"
            f"count-objects -v:\n{run('count-objects', '-v')}\n"
            f".git/objects listing:\n{listing(objects) if objects.is_dir() else '(absent)'}\n"
            f"GIT_* environment:\n{env or '(none)'}"
        )
    except Exception as exc:  # noqa: BLE001 - never mask the original git failure
        return f"<forensics capture failed: {exc!r}>"


def _git(repo: Path, *args: str) -> str:
    argv = ["git", "-C", str(repo), *args]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        failure = _GitFailed(proc.returncode, argv, output=proc.stdout, stderr=proc.stderr)
        failure.forensics = _forensics(repo)
        raise failure
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
    _git(repo, "config", "gc.autoDetach", "false")
    _git(repo, "config", "maintenance.autoDetach", "false")

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


def _fixture_batched_map(
    ticket_dir: str,
    *,
    repo_root: str | None = None,
    resolve: Callable[..., dict[str, str]] | None = None,
) -> dict[str, str]:
    """Resolve the fixture map, recovering from a one-shot Git read transient."""
    resolver = resolve or authorship.build_ticket_position_commit_map
    for _attempt in range(5):
        position_map = resolver(ticket_dir, repo_root=repo_root)
        if position_map:
            return position_map
    repo = Path(ticket_dir).parent
    raise AssertionError(
        f"batched map remained empty after 5 attempts\nobject-store forensics:\n{_forensics(repo)}"
    )


def _fixture_per_event_commit(
    position: str,
    ticket_dir: str,
    *,
    repo_root: str | None = None,
    resolve: Callable[..., str | None] | None = None,
) -> str:
    """Resolve one fixture event, recovering from a transient fail-closed Git read."""
    resolver = resolve or authorship.resolve_event_commit
    for _attempt in range(5):
        commit = resolver(position, ticket_dir, repo_root=repo_root)
        if commit is not None:
            return commit
    repo = Path(ticket_dir).parent
    raise AssertionError(
        f"per-event commit for {position} remained unresolved after 5 attempts\n"
        f"object-store forensics:\n{_forensics(repo)}"
    )


def test_batched_map_matches_the_per_event_resolver_for_every_event(tracker: Path) -> None:
    """The equivalence property: identical commit attribution for every event of a
    realistic ticket."""
    ticket_dir = str(tracker / TICKET)
    batched = _fixture_batched_map(ticket_dir)

    positions = _positions(tracker)
    assert len(positions) == 8  # 2 base + 2 side + 2 mainline + 1 shared + 1 post-merge

    for position in positions:
        expected = _fixture_per_event_commit(position, ticket_dir)
        assert batched.get(position) == expected, position


@pytest.mark.parametrize("mode", ["rc128", "empty", "eagain"])
def test_fixture_batched_map_recovers_from_one_transient_git_read(
    tracker: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """A one-shot process/Git transient must not fail the fixture equivalence oracle."""
    real_run = subprocess.run
    broad_calls = 0
    narrow_calls = 0

    def transient_once(cmd, *args, **kwargs):
        nonlocal broad_calls, narrow_calls
        is_broad = isinstance(cmd, list) and "--format=%x1e%H" in cmd
        is_narrow = isinstance(cmd, list) and "--format=%H" in cmd and "--format=%x1e%H" not in cmd
        if not is_broad and not is_narrow:
            return real_run(cmd, *args, **kwargs)
        if is_broad:
            broad_calls += 1
            transient = broad_calls == 1
        else:
            narrow_calls += 1
            transient = narrow_calls == 1
        if transient:
            if mode == "rc128":
                return subprocess.CompletedProcess(
                    cmd, 128, stdout="", stderr="fatal: transient read"
                )
            if mode == "empty":
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            raise BlockingIOError(11, "Resource temporarily unavailable")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(authorship.subprocess, "run", transient_once)
    ticket_dir = str(tracker / TICKET)

    batched = _fixture_batched_map(ticket_dir)

    positions = _positions(tracker)
    assert broad_calls == 2
    assert set(batched) == set(positions)
    for position in positions:
        assert batched[position] == _fixture_per_event_commit(position, ticket_dir)
    assert narrow_calls == len(positions) + 1


def test_fixture_batched_map_persistent_failure_is_bounded_and_diagnostic(
    tracker: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Persistent failure stays loud and bounded rather than becoming an infinite retry."""
    calls = 0

    def always_empty(*args, **kwargs):
        nonlocal calls
        calls += 1
        return {}

    monkeypatch.setattr(authorship, "build_ticket_position_commit_map", always_empty)

    with pytest.raises(AssertionError, match="batched map remained empty after 5 attempts") as exc:
        _fixture_batched_map(str(tracker / TICKET))

    assert calls == 5
    assert "fsck --full:" in str(exc.value)
    assert "count-objects -v:" in str(exc.value)


def test_fixture_per_event_failure_is_bounded_and_diagnostic(tracker: Path) -> None:
    """Persistent per-event failure stays finite and retains object-store evidence."""
    calls = 0

    def always_unresolved(*args, **kwargs):
        nonlocal calls
        calls += 1
        return None

    with pytest.raises(
        AssertionError,
        match="per-event commit for 1000-aaaa1111 remained unresolved after 5 attempts",
    ) as exc:
        _fixture_per_event_commit(
            "1000-aaaa1111",
            str(tracker / TICKET),
            resolve=always_unresolved,
        )

    assert calls == 5
    assert "fsck --full:" in str(exc.value)
    assert "count-objects -v:" in str(exc.value)


def test_the_off_first_parent_add_is_resolved_to_the_older_side_branch_commit(
    tracker: Path,
) -> None:
    """The case ``--full-history`` exists for, asserted explicitly rather than left to the
    sweep above: the shared event's introducing commit is the SIDE-BRANCH add, which is
    off the first-parent chain, and it is what BOTH resolvers return."""
    ticket_dir = str(tracker / TICKET)
    position = "1500-shared01"

    batched = _fixture_batched_map(ticket_dir)[position]
    per_event = _fixture_per_event_commit(position, ticket_dir)
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
        expected = _fixture_per_event_commit(position, str(ticket_dir))
        assert entry["position"]["commit_sha"] == expected, position


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

    real_run = subprocess.run
    broad_calls = 0

    def transient_broad_read_once(cmd, *args, **kwargs):
        nonlocal broad_calls
        is_broad = isinstance(cmd, list) and "--format=%x1e%H" in cmd
        if not is_broad:
            return real_run(cmd, *args, **kwargs)
        broad_calls += 1
        if broad_calls == 1:
            return subprocess.CompletedProcess(cmd, 128, stdout="", stderr="fatal: transient read")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(authorship.subprocess, "run", transient_broad_read_once)

    real_batched_resolve = authorship.build_ticket_position_commit_map

    def stable_fixture_resolve(ticket_dir: str, *, repo_root: str | None = None) -> dict[str, str]:
        return _fixture_batched_map(
            ticket_dir,
            repo_root=repo_root,
            resolve=real_batched_resolve,
        )

    monkeypatch.setattr(
        authorship,
        "build_ticket_position_commit_map",
        stable_fixture_resolve,
    )

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
    assert broad_calls == 2
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


def test_a_git_failure_captures_object_store_forensics(tmp_path: Path) -> None:
    """A failing ``_git`` snapshots the OBJECT STORE, not just git's message.

    Bug innovative-dandruffy-deer: on CI (twice, months apart, xdist worker gw1) the
    fixture's merge failed with ``invalid object ... git write-tree failed to write a
    tree`` — a freshly committed loose blob GONE from ``.git/objects`` two subprocess
    calls after ``git commit`` wrote it. Injection experiments proved the class (only
    ENOENT on the loose object file reproduces that exact signature; a corrupt-but-present
    object does not), but git's own stderr cannot say WHY the file is absent. So the
    moment a git command fails, the exception must carry the evidence the next occurrence
    needs: ``git fsck`` (names every missing object), ``git count-objects -v``, a
    recursive listing of ``.git/objects`` with sizes and mtimes (shows what survived and
    when it was written), and the ``GIT_*`` environment (rules redirection in or out).

    The reproduction below is that proven mechanism: build the fixture's merge topology,
    delete the mainline blob's loose object file, merge.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "tickets")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "a.json").write_text("a")
    _git(repo, "add", "a.json")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "s.json").write_text("s")
    _git(repo, "add", "s.json")
    _git(repo, "commit", "-q", "-m", "side")
    _git(repo, "checkout", "-q", "tickets")
    (repo / "b.json").write_text("b")
    _git(repo, "add", "b.json")
    _git(repo, "commit", "-q", "-m", "mainline")
    blob = _git(repo, "rev-parse", "HEAD:b.json")
    head = _git(repo, "rev-parse", "HEAD")
    (repo / ".git" / "objects" / blob[:2] / blob[2:]).unlink()

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        _git(repo, "merge", "-q", "--no-edit", "side")

    rendered = str(excinfo.value)
    # The CI signature itself is preserved...
    assert f"invalid object 100644 {blob}" in rendered
    # ...and the forensics answer the questions the CI log could not:
    assert f"missing blob {blob}" in rendered, "fsck must name the missing object"
    assert "count:" in rendered, "count-objects -v output must be captured"
    assert f"{head[:2]}/{head[2:]}" in rendered, (
        "the .git/objects listing must show the objects that DID survive"
    )
    assert "GIT_* environment:" in rendered, "the GIT_* env snapshot must be present"
