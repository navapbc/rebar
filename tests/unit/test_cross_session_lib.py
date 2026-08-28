"""Library ``CrossSessionWarning`` emission (epic concave-pale-sheldrake, story
3d8a / Library: warnings.warn CrossSessionWarning from single-ticket functions).

Full held-out oracle for story S5. Asserts observable behavior only — the presence,
count, category, and message of warnings emitted on the stdlib ``warnings`` channel, and
that the underlying library call still returns normally. Never asserts internal structure.

Setup mirrors ``tests/unit/test_cross_session.py``: a real temporary tracker, acting
session controlled via ``REBAR_SESSION_ID``; claim as holder A, then act as B.
"""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.unit

_SESSION_VARS = ("REBAR_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "OPENCODE_SESSION_ID", "SESSION_ID")


@pytest.fixture
def rebar_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for var in _SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("AI_AGENT", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    from rebar.config import reset_config_cache

    reset_config_cache()
    return repo


def _claim_as_a(repo: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    """Create + claim a ticket as session A; return its id. Caller then acts as B."""
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-A")
    tid = rebar.create_ticket("task", "t", repo_root=str(repo))
    rebar.claim(tid, assignee="alice", repo_root=str(repo))
    return tid


def _act_as_b(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_SESSION_ID", "sess-B")
    from rebar.config import reset_config_cache

    reset_config_cache()


def _cross_session_warnings(rec) -> list:
    return [w for w in rec if issubclass(w.category, rebar.CrossSessionWarning)]


# ---------------------------------------------------------------- happy path (implementer-visible)
def test_show_ticket_warns_for_other_session(rebar_repo, monkeypatch) -> None:
    """B calling show_ticket on an A-held ticket emits exactly one CrossSessionWarning
    naming holder A; the read still returns the ticket state."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)
    assert state is not None


def test_comment_warns_and_still_mutates(rebar_repo, monkeypatch) -> None:
    """B commenting on an A-held ticket emits a CrossSessionWarning AND the comment lands."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.comment(tid, "hello from B", repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)
    # The mutation applied: the comment is present in the ticket's event stream.
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    dumped = str(state)
    assert "hello from B" in dumped


def test_same_session_is_silent(rebar_repo, monkeypatch) -> None:
    """Acting as the SAME session A that holds the ticket emits no CrossSessionWarning."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    # stay as session A
    from rebar.config import reset_config_cache

    reset_config_cache()
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.show_ticket(tid, repo_root=str(rebar_repo))
        rebar.comment(tid, "self note", repo_root=str(rebar_repo))
    assert _cross_session_warnings(rec) == []


# ---------------------------------------------------------------- held out
def test_transition_warns_for_other_session(rebar_repo, monkeypatch) -> None:
    """A single-ticket write (transition in_progress->closed) by B warns about holder A
    (computed pre-mutation), and the transition still applies."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert "closed" in str(state)


def test_link_warns_for_primary_endpoint(rebar_repo, monkeypatch) -> None:
    """A two-endpoint op (link id1,id2) by B warns for the PRIMARY endpoint id1 (A-held),
    not id2; exactly one warning naming holder A."""
    id1 = _claim_as_a(rebar_repo, monkeypatch)
    # id2 created but unheld (session A context is fine; it is not claimed).
    id2 = rebar.create_ticket("task", "other", repo_root=str(rebar_repo))
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.link(id1, id2, "relates_to", repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)


def test_bulk_reads_never_warn(rebar_repo, monkeypatch) -> None:
    """Bulk reads (list_tickets, ready) as B emit no CrossSessionWarning even with an
    A-held in_progress ticket present."""
    _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.list_tickets(repo_root=str(rebar_repo))
        rebar.ready(repo_root=str(rebar_repo))
    assert _cross_session_warnings(rec) == []


def test_unset_session_is_silent(rebar_repo, monkeypatch) -> None:
    """With REBAR_SESSION_ID unset, the acting session is unknown, so no warning fires."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    for var in _SESSION_VARS:
        monkeypatch.delenv(var, raising=False)
    from rebar.config import reset_config_cache

    reset_config_cache()
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert _cross_session_warnings(rec) == []


def test_warning_is_crosssessionwarning_subclass(rebar_repo, monkeypatch) -> None:
    """The emitted warning is an instance of rebar.CrossSessionWarning, itself a
    UserWarning subclass, so a caller can filter it distinctly."""
    assert issubclass(rebar.CrossSessionWarning, UserWarning)
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with pytest.warns(rebar.CrossSessionWarning):
        rebar.show_ticket(tid, repo_root=str(rebar_repo))


def test_reentrancy_exactly_one_warning(rebar_repo, monkeypatch) -> None:
    """The detector internally calls the public show_ticket; the re-entrancy guard must
    ensure exactly ONE CrossSessionWarning per top-level show_ticket call (not N, and no
    unbounded recursion)."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert len(_cross_session_warnings(rec)) == 1


def test_best_effort_detector_raises_still_returns(rebar_repo, monkeypatch) -> None:
    """When the detector raises, the library call still returns normally and emits no
    CrossSessionWarning — the advisory warning never breaks the operation."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    _act_as_b(monkeypatch)

    def _boom(*a, **k):
        raise RuntimeError("detector blew up")

    monkeypatch.setattr(
        "rebar._commands.cross_session.cross_session_warning_for", _boom, raising=True
    )
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state is not None
    assert _cross_session_warnings(rec) == []


def test_unlink_warns_for_primary_endpoint(rebar_repo, monkeypatch) -> None:
    """unlink(id1, id2) by B warns for the PRIMARY endpoint id1 (A-held), matching link:
    exactly one warning naming holder A."""
    id1 = _claim_as_a(rebar_repo, monkeypatch)
    id2 = rebar.create_ticket("task", "other", repo_root=str(rebar_repo))
    rebar.link(id1, id2, "relates_to", repo_root=str(rebar_repo))  # as A: silent
    _act_as_b(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.unlink(id1, id2, "relates_to", repo_root=str(rebar_repo))
    xs = _cross_session_warnings(rec)
    assert len(xs) == 1
    assert "sess-A" in str(xs[0].message)


def _force_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the merged detector to always report a cross-session holder, so each
    instrumented wrapper's emit call fires deterministically regardless of real session
    state — isolating "does this wrapper call the emit helper exactly once?"."""
    monkeypatch.setattr(
        "rebar._commands.cross_session.cross_session_warning_for",
        lambda ticket_id, repo_root=None: "ticket held by another session sess-A",
        raising=True,
    )


def _fresh(repo: Path) -> str:
    return rebar.create_ticket("task", "t", repo_root=str(repo))


def _setup_plain(repo: Path) -> str:
    return _fresh(repo)


def _setup_tagged(repo: Path) -> str:
    tid = _fresh(repo)
    rebar.tag(tid, "x", repo_root=str(repo))
    return tid


# Each entry: (name, setup(repo)->tid, action(repo, tid)). The action is the single
# instrumented library call under test; setup does any un-recorded prep. Covers the emit
# sites the happy/held-out cases above do not exercise directly (gates + the remaining
# mutations/reads), so a wrapper that forgot the emit — or fired it twice — is caught.
_EMIT_SITES = [
    ("deps", _setup_plain, lambda r, t: rebar.deps(t, repo_root=str(r))),
    (
        "transition",
        _setup_plain,
        lambda r, t: rebar.transition(t, "open", "in_progress", repo_root=str(r)),
    ),
    ("edit_ticket", _setup_plain, lambda r, t: rebar.edit_ticket(t, title="new", repo_root=str(r))),
    ("tag", _setup_plain, lambda r, t: rebar.tag(t, "y", repo_root=str(r))),
    ("untag", _setup_tagged, lambda r, t: rebar.untag(t, "x", repo_root=str(r))),
    ("archive", _setup_plain, lambda r, t: rebar.archive(t, repo_root=str(r))),
    (
        "set_file_impact",
        _setup_plain,
        lambda r, t: rebar.set_file_impact(t, [{"path": "a.py", "reason": "r"}], repo_root=str(r)),
    ),
    (
        "declare_no_file_impact",
        _setup_plain,
        lambda r, t: rebar.declare_no_file_impact(
            t, "no source changes are required", repo_root=str(r)
        ),
    ),
    ("check_ac", _setup_plain, lambda r, t: rebar.check_ac(t, repo_root=str(r))),
    ("clarity_check", _setup_plain, lambda r, t: rebar.clarity_check(t, repo_root=str(r))),
]


@pytest.mark.parametrize(("name", "setup", "action"), _EMIT_SITES, ids=[s[0] for s in _EMIT_SITES])
def test_instrumented_function_emits_exactly_once(
    rebar_repo, monkeypatch, name, setup, action
) -> None:
    """Every instrumented single-ticket entry point emits EXACTLY ONE CrossSessionWarning
    per call when the detector reports a holder — not zero (forgot to instrument) and not
    two (delegated to another instrumented function without the guard spanning it)."""
    tid = setup(rebar_repo)
    _force_detector(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        action(rebar_repo, tid)
    assert len(_cross_session_warnings(rec)) == 1, name


def test_reopen_emits_exactly_once(rebar_repo, monkeypatch) -> None:
    """reopen() delegates to transition(); both are instrumented, so without care a single
    reopen would emit TWO warnings (the guard resets between them). With the detector
    forced to report a holder, exactly one CrossSessionWarning must fire per reopen."""
    tid = _claim_as_a(rebar_repo, monkeypatch)
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))  # as A: silent
    _force_detector(monkeypatch)
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        rebar.reopen(tid, repo_root=str(rebar_repo))
    assert len(_cross_session_warnings(rec)) == 1


def test_mcp_build_server_does_not_globally_silence_cross_session_warning(monkeypatch) -> None:
    """build_server() must NOT install a process-wide ignore filter for CrossSessionWarning:
    that would silence the advisory for EVERY in-process `import rebar` caller, not just the
    MCP response path. Observably, an independent library emit AFTER build_server() must still
    surface. (RED against a global filterwarnings('ignore', CrossSessionWarning) in
    build_server, which prepends an ignore that suppresses this warn.)"""
    pytest.importorskip("mcp")
    from rebar import mcp_server

    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        mcp_server.build_server()
        warnings.warn("independent import-rebar consumer", rebar.CrossSessionWarning, stacklevel=2)
    assert len(_cross_session_warnings(rec)) == 1


def test_double_advisory_proxy_scopes_suppression_to_the_handler(monkeypatch) -> None:
    """The MCP surface dedups by running each registered tool under
    suppress_cross_session_warning() (a scoped guard) instead of a process-global filter, so
    the library's own CrossSessionWarning does not double the tool's response field. The
    suppression is scoped to the handler: an identical emit OUTSIDE the handler still fires."""
    from rebar._lib_warn import emit_cross_session_warning, suppress_library_double_advisory

    _force_detector(monkeypatch)

    class _FakeMCP:
        def tool(self, *_a, **_k):
            def _decorate(fn):
                return fn

            return _decorate

    proxy = suppress_library_double_advisory(_FakeMCP())

    @proxy.tool(annotations={})
    def handler() -> None:
        emit_cross_session_warning("t")

    with warnings.catch_warnings(record=True) as rec_in:
        warnings.simplefilter("always")
        handler()
    assert _cross_session_warnings(rec_in) == []  # suppressed within the handler

    with warnings.catch_warnings(record=True) as rec_out:
        warnings.simplefilter("always")
        emit_cross_session_warning("t")
    assert len(_cross_session_warnings(rec_out)) == 1  # not suppressed outside it
