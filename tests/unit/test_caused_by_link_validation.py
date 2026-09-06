"""``caused_by`` link-time validation (story dormant-fibre-pterosaurs).

``rebar link <bug> <target> caused_by`` used to accept ANY target, so the store's only
machine-readable causation edge could point at a ticket that never shipped a line of code.
The rule under test: a ``caused_by`` target must have at least one commit referencing it
(a ``rebar-ticket:`` trailer or a leading ``<id>:`` subject), unless the caller forces.

The tests assert OBSERVABLE behaviour only — the raised error, the recorded ``deps``, the
``--dry-run`` stdout — never a private symbol name or a source spelling.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.unit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _bare_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


@pytest.fixture
def repo_with_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store whose CODE repo has real commits, so the scan can reach a verdict."""
    repo = _bare_repo(tmp_path, monkeypatch, "repo")
    _git(repo, "commit", "--allow-empty", "-q", "-m", "root commit")
    return repo


@pytest.fixture
def repo_without_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A store whose CODE repo has NO commits: the scan cannot reach a verdict."""
    return _bare_repo(tmp_path, monkeypatch, "empty")


def _relations(tid: str, repo: Path) -> list[tuple[str, str]]:
    return [
        (d["relation"], d["target_id"]) for d in rebar.show_ticket(tid, repo_root=str(repo))["deps"]
    ]


def _culprit_with_commit(repo: Path) -> str:
    """A ticket whose introducing commit carries its ``rebar-ticket:`` trailer."""
    culprit = rebar.create_ticket("task", "the change that broke it", repo_root=str(repo))
    _git(
        repo,
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        f"do the thing\n\nrebar-ticket: {culprit}",
    )
    return culprit


def test_caused_by_to_a_target_with_a_commit_is_recorded(repo_with_history: Path) -> None:
    repo = repo_with_history
    culprit = _culprit_with_commit(repo)
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_caused_by_to_a_commitless_target_is_refused_and_writes_nothing(
    repo_with_history: Path,
) -> None:
    repo = repo_with_history
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    with pytest.raises(rebar.RebarError) as excinfo:
        rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    message = str(excinfo.value)
    assert culprit in message, f"the refusal must name the target it rejected: {message}"
    assert "--force" in message, f"the refusal must name the escape hatch: {message}"
    assert ("caused_by", culprit) not in _relations(bug, repo), "a refused link left an edge"


def test_force_records_the_edge_the_rule_would_refuse(repo_with_history: Path) -> None:
    repo = repo_with_history
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, culprit, "caused_by", force="attribution by scope of work", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_other_relations_to_the_same_target_are_unaffected(repo_with_history: Path) -> None:
    repo = repo_with_history
    other = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, other, "relates_to", repo_root=str(repo))

    assert ("relates_to", other) in _relations(bug, repo)


def test_unreadable_history_allows_the_link(repo_without_history: Path) -> None:
    """A clone with no commits cannot distinguish "no commit" from "no history",
    so the link is allowed: a refusal must never mean "this checkout is empty"."""
    repo = repo_without_history
    culprit = rebar.create_ticket("task", "unverifiable", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_dry_run_previews_the_refusal_and_writes_nothing(
    repo_with_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rebar._commands.link_revert import link_cli

    repo = repo_with_history
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rc = link_cli([bug, culprit, "caused_by", "--dry-run"], repo_root=str(repo))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Would reject" in out, f"dry run must preview the refusal, not a create: {out}"
    assert culprit in out
    assert ("caused_by", culprit) not in _relations(bug, repo)


def test_dry_run_under_force_previews_the_create(
    repo_with_history: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from rebar._commands.link_revert import link_cli

    repo = repo_with_history
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rc = link_cli([bug, culprit, "caused_by", "--dry-run", "--force=scope"], repo_root=str(repo))

    out = capsys.readouterr().out
    assert rc == 0
    assert "Would create" in out, f"a forced dry run must not preview a refusal: {out}"


def test_close_precheck_scan_still_reports_an_empty_list_when_history_is_unreadable(
    tmp_path: Path,
) -> None:
    """The close gate's contract is ``[]`` for "no referencing commit", including when git
    cannot be read at all — the hoisted scan's ``None`` must not leak into it."""
    from rebar._commands import close_precheck

    assert close_precheck._referencing_commits({"x"}, str(tmp_path), str(tmp_path)) == []


# ---------------------------------------------------------------------------
# Bug ambitious-creative-ovenbird: a checkout that LAGS is not a checkout that
# proves absence. The scan used to walk HEAD only, so the rebar MCP server's
# code clone — cloned once at container boot, thereafter advanced only on its
# remote-tracking refs by the snapshot machinery's fetches — refused correct,
# evidence-backed links and offered `--force` as the sole remedy.
# ---------------------------------------------------------------------------


def _branch(repo: Path) -> str:
    return subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _culprit_reachable_only_from_a_remote_ref(repo: Path) -> str:
    """A ticket whose introducing commit is OFF this checkout's HEAD.

    Models the MCP server's code clone exactly: the commit is in the object database and
    reachable from ``refs/remotes/origin/main``, but the branch HEAD points at was never
    advanced past the clone-time commit.
    """
    culprit = rebar.create_ticket("task", "landed upstream", repo_root=str(repo))
    branch = _branch(repo)
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(repo, "commit", "--allow-empty", "-q", "-m", f"do the thing\n\nrebar-ticket: {culprit}")
    landed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    _git(repo, "update-ref", f"refs/remotes/origin/{branch}", landed)
    _git(repo, "reset", "--soft", before)
    return culprit


def test_a_commit_reachable_only_from_a_remote_ref_is_not_refused(
    repo_with_history: Path,
) -> None:
    """AC2. The evidence is in this checkout — just not on HEAD — so the link stands."""
    repo = repo_with_history
    culprit = _culprit_reachable_only_from_a_remote_ref(repo)
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_mcp_link_tickets_records_the_edge_when_the_checkout_lags(
    repo_with_history: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC6. Proven through the MCP tool itself, which is where the defect was observed.

    The tool takes no ``repo_root``: it resolves the code checkout from ``REBAR_ROOT``,
    which on the server is the lagging clone. A library-only test would not exercise that.
    """
    import logging

    from rebar import _mcp_writes

    repo = repo_with_history
    culprit = _culprit_reachable_only_from_a_remote_ref(repo)
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))
    monkeypatch.setenv("REBAR_ROOT", str(repo))

    tools: dict[str, object] = {}

    class _FakeMCP:
        def tool(self, *_a, **_k):
            def _decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return _decorate

    class _FakeCtx:
        logger = logging.getLogger("test")

        @staticmethod
        def readonly() -> bool:
            return False

        @staticmethod
        def dump(obj):
            return obj

        @staticmethod
        def allow_llm() -> bool:
            return False

    _mcp_writes.register_write_tools(_FakeMCP(), ctx=_FakeCtx())

    out = tools["link_tickets"](bug, culprit, "caused_by")  # type: ignore[operator]

    assert out.result.startswith("ok")
    assert ("caused_by", culprit) in _relations(bug, repo)


def test_a_commit_present_only_upstream_is_consulted_before_refusing(
    repo_with_history: Path, tmp_path: Path
) -> None:
    """AC3. The commit is absent from the scanning checkout entirely — a refusal would be a
    claim about the world made from a view that never contained the evidence."""
    repo = repo_with_history
    culprit = rebar.create_ticket("task", "landed upstream", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    upstream = tmp_path / "upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", _branch(repo))
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    _git(
        upstream,
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        f"upstream fix\n\nrebar-ticket: {culprit}",
    )
    _git(repo, "remote", "add", "origin", str(upstream))

    rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_an_unreachable_remote_leaves_absence_unproven_and_allows_the_link(
    repo_with_history: Path, tmp_path: Path
) -> None:
    """A configured remote this checkout cannot reach is the same epistemic position as an
    unreadable history: absence is not established, so the guard must not refuse."""
    repo = repo_with_history
    culprit = rebar.create_ticket("task", "unverifiable", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))
    _git(repo, "remote", "add", "origin", str(tmp_path / "definitely-not-a-repo"))

    rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)


def test_close_precheck_scan_still_ignores_a_commit_off_head(repo_with_history: Path) -> None:
    """AC5. The close gate's contract is "reachable from your worktree's current HEAD".
    Widening the caused_by scan must not widen that one."""
    from rebar._commands import close_precheck

    repo = repo_with_history
    culprit = _culprit_reachable_only_from_a_remote_ref(repo)

    found = close_precheck._referencing_commits(
        {culprit}, str(repo / ".tickets-tracker"), str(repo)
    )

    assert found == [], "the close gate's HEAD-scoped scan was silently widened"


def _reachable_remote(repo: Path, tmp_path: Path) -> Path:
    """A real, fetchable upstream for ``repo`` that references nothing."""
    upstream = tmp_path / "reachable-upstream"
    upstream.mkdir()
    _git(upstream, "init", "-q", "-b", _branch(repo))
    _git(upstream, "config", "user.email", "test@example.com")
    _git(upstream, "config", "user.name", "Test")
    _git(upstream, "commit", "--allow-empty", "-q", "-m", "unrelated upstream work")
    _git(repo, "remote", "add", "origin", str(upstream))
    return upstream


def test_a_refreshed_checkout_still_refuses_a_genuinely_commitless_target(
    repo_with_history: Path, tmp_path: Path
) -> None:
    """The other direction, and the load-bearing one. Once the checkout HAS been brought
    current against a reachable remote, its silence is evidence again — so the guard must
    still refuse. A fix that only ever converts refusals into allows is worse than the
    defect it replaces."""
    repo = repo_with_history
    _reachable_remote(repo, tmp_path)
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    with pytest.raises(rebar.RebarError) as excinfo:
        rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert culprit in str(excinfo.value)
    assert ("caused_by", culprit) not in _relations(bug, repo), "a refused link left an edge"


def test_mcp_link_tickets_still_refuses_a_commitless_target_after_a_refresh(
    repo_with_history: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same both-directions proof at the surface the defect was reported on."""
    import logging

    from rebar import _mcp_writes

    repo = repo_with_history
    _reachable_remote(repo, tmp_path)
    culprit = rebar.create_ticket("task", "never shipped", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))
    monkeypatch.setenv("REBAR_ROOT", str(repo))

    tools: dict[str, object] = {}

    class _FakeMCP:
        def tool(self, *_a, **_k):
            def _decorate(fn):
                tools[fn.__name__] = fn
                return fn

            return _decorate

    class _FakeCtx:
        logger = logging.getLogger("test")

        @staticmethod
        def readonly() -> bool:
            return False

        @staticmethod
        def dump(obj):
            return obj

        @staticmethod
        def allow_llm() -> bool:
            return False

    _mcp_writes.register_write_tools(_FakeMCP(), ctx=_FakeCtx())

    with pytest.raises(rebar.RebarError):
        tools["link_tickets"](bug, culprit, "caused_by")  # type: ignore[operator]

    assert ("caused_by", culprit) not in _relations(bug, repo)


def test_an_unanswerable_remote_probe_is_indeterminate_not_a_refusal(
    repo_with_history: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A checkout that cannot even be asked whether it has a remote has said nothing. A
    refusal there would offer ``--force`` as the only way forward for a checkout fault."""
    import rebar._store.gitutil as gitutil

    repo = repo_with_history
    culprit = rebar.create_ticket("task", "unverifiable", repo_root=str(repo))
    bug = rebar.create_ticket("bug", "it broke", repo_root=str(repo))

    real = gitutil.run_git

    def _hang_on_the_probe(cwd, *args, **kwargs):
        if args[:1] == ("remote",):
            raise subprocess.TimeoutExpired(cmd="git remote", timeout=1)
        return real(cwd, *args, **kwargs)

    monkeypatch.setattr(gitutil, "run_git", _hang_on_the_probe)

    rebar.link(bug, culprit, "caused_by", repo_root=str(repo))

    assert ("caused_by", culprit) in _relations(bug, repo)
