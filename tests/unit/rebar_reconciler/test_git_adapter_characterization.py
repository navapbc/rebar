"""Characterization test pinning ``rebar_reconciler.git_adapter``'s behaviour.

The reconciler routes *every* ``git`` subprocess through :mod:`git_adapter` (the
single git seam). This test documents "this is the git behaviour the adapter
produces" — the exact ``git`` argv each op constructs and the exact values of the
named ref/path constants — so any future drift in the adapter's git invocations,
or in the tickets-tracker / bridge-state paths lifted out of the reconciler's
commit-back path, is caught by a failing test rather than shipping silently.

It is a *characterization* (golden) test: it asserts the observable contract of a
migration that previously had no such pin — the byte-for-byte argv the inline
``subprocess.run(["git", …])`` calls used before they were centralised here.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

import pytest

from rebar_reconciler import git_adapter

# ---------------------------------------------------------------------------
# Named ref / path constants — the single source of truth for the strings the
# commit-back path (reconcile._commit_binding_store_snapshot) stages. Drift in
# any of these is a behaviour change (a differently-named branch or a store
# written to the wrong path) and must fail loudly.
# ---------------------------------------------------------------------------


def test_ref_and_path_constants_are_pinned() -> None:
    assert git_adapter.TICKETS_BRANCH == "tickets"
    assert git_adapter.TICKETS_REF == "refs/heads/tickets"
    assert git_adapter.TRACKER_DIR == ".tickets-tracker"
    assert git_adapter.BRIDGE_STATE_DIR == ".bridge_state"
    assert git_adapter.BINDINGS_FILE == ".bridge_state/bindings.json"
    assert git_adapter.BINDINGS_RETIRED_FILE == ".bridge_state/bindings-retired.json"
    # The bindings files live under the bridge-state dir (relationship, not just values).
    assert git_adapter.BINDINGS_FILE.startswith(git_adapter.BRIDGE_STATE_DIR + "/")
    assert git_adapter.BINDINGS_RETIRED_FILE.startswith(git_adapter.BRIDGE_STATE_DIR + "/")
    # TICKETS_REF is derived from TICKETS_BRANCH — they can't drift apart.
    assert git_adapter.TICKETS_REF == f"refs/heads/{git_adapter.TICKETS_BRANCH}"


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Replace the shared ``run_git`` seam with a spy returning a benign result.

    Every git_adapter op delegates to the module-level ``run_git`` name; patching
    it lets us assert the exact positional argv (git subcommand + args) and the
    keyword posture (``check``/``env``/``timeout``) each op passes — with no git
    process ever spawned.
    """
    result = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    fake = MagicMock(return_value=result)
    monkeypatch.setattr(git_adapter, "run_git", fake)
    return fake


REPO = "/repo/root"


def test_rev_parse_argv(spy: MagicMock) -> None:
    git_adapter.rev_parse(REPO, git_adapter.TICKETS_REF)
    spy.assert_called_once_with(REPO, "rev-parse", "refs/heads/tickets", check=False, text=True)


def test_cat_file_exists_argv(spy: MagicMock) -> None:
    assert git_adapter.cat_file_exists(REPO, "tickets:.bridge_state/bindings.json") is True
    spy.assert_called_once_with(
        REPO, "cat-file", "-e", "tickets:.bridge_state/bindings.json", check=False
    )


def test_diff_cached_names_argv(spy: MagicMock) -> None:
    git_adapter.diff_cached_names(REPO)
    spy.assert_called_once_with(REPO, "diff", "--cached", "--name-only", check=True)


def test_read_tree_argv(spy: MagicMock) -> None:
    env = {"GIT_INDEX_FILE": "/tmp/idx"}
    git_adapter.read_tree(REPO, "HEAD^{tree}", env=env)
    spy.assert_called_once_with(REPO, "read-tree", "HEAD^{tree}", check=True, env=env)


def test_rm_cached_argv_with_ignore_unmatch(spy: MagicMock) -> None:
    git_adapter.rm_cached(REPO, "a.json", "b.json")
    spy.assert_called_once_with(
        REPO, "rm", "--cached", "--ignore-unmatch", "a.json", "b.json", check=True, env=None
    )


def test_rm_cached_argv_without_ignore_unmatch(spy: MagicMock) -> None:
    git_adapter.rm_cached(REPO, "a.json", ignore_unmatch=False)
    spy.assert_called_once_with(REPO, "rm", "--cached", "a.json", check=True, env=None)


def test_write_tree_argv(spy: MagicMock) -> None:
    git_adapter.write_tree(REPO)
    spy.assert_called_once_with(REPO, "write-tree", check=True, env=None)


def test_commit_tree_argv(spy: MagicMock) -> None:
    git_adapter.commit_tree(REPO, "TREEOID", parent="PARENTOID", message="msg")
    spy.assert_called_once_with(
        REPO,
        "commit-tree",
        "TREEOID",
        "-p",
        "PARENTOID",
        "-m",
        "msg",
        check=True,
        env=None,
    )


def test_update_ref_argv_cas(spy: MagicMock) -> None:
    git_adapter.update_ref(REPO, git_adapter.TICKETS_REF, "NEW", "OLD")
    spy.assert_called_once_with(REPO, "update-ref", "refs/heads/tickets", "NEW", "OLD", check=True)


def test_update_ref_argv_no_old(spy: MagicMock) -> None:
    git_adapter.update_ref(REPO, git_adapter.TICKETS_REF, "NEW")
    spy.assert_called_once_with(REPO, "update-ref", "refs/heads/tickets", "NEW", check=True)


def test_add_argv_stages_named_paths_only(spy: MagicMock) -> None:
    # The commit-back path stages exactly the two binding stores — never ``git add -A``.
    git_adapter.add(REPO, git_adapter.BINDINGS_FILE, git_adapter.BINDINGS_RETIRED_FILE)
    spy.assert_called_once_with(
        REPO,
        "add",
        ".bridge_state/bindings.json",
        ".bridge_state/bindings-retired.json",
        check=True,
    )


def test_commit_argv_no_verify_quiet(spy: MagicMock) -> None:
    git_adapter.commit(REPO, "reconciler: persist", no_verify=True, quiet=True)
    spy.assert_called_once_with(
        REPO, "commit", "--no-verify", "-q", "-m", "reconciler: persist", check=True
    )


def test_commit_argv_plain(spy: MagicMock) -> None:
    git_adapter.commit(REPO, "msg")
    spy.assert_called_once_with(REPO, "commit", "-m", "msg", check=True)


def test_log_format_argv(spy: MagicMock) -> None:
    git_adapter.log_format(REPO, "SHA", "%H")
    spy.assert_called_once_with(REPO, "log", "-1", "SHA", "--format=%H", check=False, timeout=None)


def test_remote_get_url_argv(spy: MagicMock) -> None:
    git_adapter.remote_get_url(REPO, "origin")
    spy.assert_called_once_with(REPO, "remote", "get-url", "origin", check=False)


def test_verify_commit_argv_with_repo(spy: MagicMock) -> None:
    git_adapter.verify_commit(REPO, "SHA")
    spy.assert_called_once_with(REPO, "verify-commit", "SHA", check=False, timeout=15)


def test_verify_commit_argv_cwd_relative_when_repo_none(spy: MagicMock) -> None:
    # repo_root=None preserves the historical CWD-relative behaviour (no ``-C``).
    git_adapter.verify_commit(None, "SHA")
    spy.assert_called_once_with(None, "verify-commit", "SHA", check=False, timeout=15)


def test_commit_email_argv(spy: MagicMock) -> None:
    git_adapter.commit_email(REPO, "SHA")
    spy.assert_called_once_with(REPO, "log", "-1", "--format=%ae", "SHA", check=False, timeout=10)
