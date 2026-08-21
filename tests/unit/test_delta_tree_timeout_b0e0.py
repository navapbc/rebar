"""Bug b0e0-7603: the ``checkout-index`` call in ``_apply_delta`` must be bounded.

``_apply_delta`` runs two git subprocesses back to back against the same temp index. The
``read-tree`` goes through :func:`rebar._snapshot.git_fetch.git_run`, bounded by
``_GIT_TIMEOUT`` (300s) — but the ``checkout-index`` (the expensive call: it writes the
actual blob content, ~620 MiB / 70k blobs for a full ticket tree) bypassed that wrapper via
``run_git`` with no ``timeout=``, so a stall on a slow or wedged volume had no upper bound
and no diagnostic.

The contract (ticket b0e0-7603): bound ``checkout-index`` by the SAME timeout its sibling
uses; a timeout is a structured failure — ``_apply_delta`` returns ``False`` so callers fall
back to full materialization (fail-closed, like every other doubt in this module) — and it
is surfaced in a diagnostic naming the operation, so a stall is distinguishable from slow
progress. ``_GIT_TIMEOUT`` itself is explicitly out of scope and pinned unchanged.

The stall test drives the REAL ``_apply_delta`` against a fake ``git`` on ``PATH`` that
sleeps on ``checkout-index`` (the 093a pattern), with the bound shrunk via monkeypatch so
the test is quick; nothing here asserts a tight elapsed time — the ceiling asserted is far
under the fake's sleep, so it can only pass because the call was cut off.

Everything here is offline: no network, no LLM.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from rebar._snapshot import delta_tree, git_fetch
from rebar._snapshot import repo_snapshot as rs


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "--quiet")
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit_all(repo: Path, msg: str = "c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture(autouse=True)
def _isolate_store(monkeypatch, tmp_path):
    store = tmp_path / "gate-tmpdir"
    store.mkdir()
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(store))


def test_stalled_checkout_index_is_cut_off_and_fails_closed(tmp_path, monkeypatch, caplog):
    """AC1 + AC3: a ``checkout-index`` that exceeds the bound is cut off — not unbounded —
    ``_apply_delta`` fails closed (``False``), and the diagnostic names the operation."""
    real_git = shutil.which("git")
    assert real_git
    fake_dir = tmp_path / "fakebin"
    fake_dir.mkdir()
    fake = fake_dir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do\n'
        '  if [ "$a" = "checkout-index" ]; then sleep 20; exit 1; fi\n'
        "done\n"
        f'exec "{real_git}" "$@"\n'
    )
    fake.chmod(0o755)

    repo = _init_repo(tmp_path / "repo")
    (repo / "a.txt").write_text("A")
    sha = _commit_all(repo, "seed")
    dest = tmp_path / "dest"
    dest.mkdir()

    monkeypatch.setenv("PATH", f"{fake_dir}{os.pathsep}{os.environ['PATH']}")
    # Shrink the bound so the test is quick; the mechanism is unchanged. Pre-fix this
    # attribute does not exist (raising=False) and no bound applies at all.
    monkeypatch.setattr(delta_tree, "_GIT_TIMEOUT", 2, raising=False)

    start = time.monotonic()
    with caplog.at_level("WARNING", logger=delta_tree.__name__):
        ok = delta_tree._apply_delta(str(repo), sha, dest, set(), {"a.txt"})
    elapsed = time.monotonic() - start

    assert ok is False, "a timed-out checkout-index must fail closed"
    # The fake git sleeps 20s; a pass under this ceiling can only mean the call was cut
    # off by the bound, never that it ran to completion.
    # timing: hang-guard — the 2s bound is 7.5x under the ceiling and the 20s sleep is 5s over it
    assert elapsed < 15, f"checkout-index ran unbounded ({elapsed:.1f}s)"
    joined = " ".join(r.message for r in caplog.records)
    assert "checkout-index" in joined and "timed out" in joined, (
        f"the timeout must be surfaced in a diagnostic naming the operation: {joined!r}"
    )


def test_timeout_falls_back_to_full_materialization(tmp_path, monkeypatch):
    """AC2: a timeout returns ``False`` from ``_apply_delta`` rather than raising out of it,
    so the caller's full-materialization fallback runs and still yields the faithful tree."""
    repo = _init_repo(tmp_path / "repo")
    (repo / "seed.txt").write_text("seed")
    _commit_all(repo, "seed")
    _git(repo, "checkout", "--quiet", "-b", "tickets")
    tdir = repo / "tickets"
    tdir.mkdir()
    for i in range(6):
        (tdir / f"t{i}.json").write_text("T" * 500)
    _commit_all(repo, "tickets base")

    first = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))

    real_run_git = delta_tree.run_git
    calls = {"checkout-index": 0}

    def timing_out_run_git(cwd, *args, **kwargs):
        if "checkout-index" in args:
            calls["checkout-index"] += 1
            raise subprocess.TimeoutExpired(cmd=["git", "checkout-index"], timeout=300)
        return real_run_git(cwd, *args, **kwargs)

    # Only the DELTA path is stubbed: repo_snapshot's full materialization reaches
    # checkout-index through git_fetch.git_run, not through delta_tree.run_git.
    monkeypatch.setattr(delta_tree, "run_git", timing_out_run_git)

    (tdir / "t0.json").write_text("T" * 499 + "X")
    sha = _commit_all(repo, "one-file delta")
    second = Path(rs.materialize_tickets("tickets", repo_root=str(repo), fetch=False))
    assert second != first

    assert calls["checkout-index"] >= 1, "the delta path must actually have fired"
    listed = sorted(_git(repo, "ls-tree", "-r", "--name-only", sha).splitlines())
    root = second / ".tickets-tracker"
    on_disk = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
    assert on_disk == listed, "the fallback full materialization must produce the faithful tree"


def test_checkout_index_shares_the_sibling_bound():
    """AC1: the bound is the SAME one the sibling ``read-tree`` call uses — one constant,
    not a second knob."""
    assert delta_tree._GIT_TIMEOUT is git_fetch._GIT_TIMEOUT


def test_git_timeout_value_unchanged():
    """AC4: bounding the call must not retune the shared constant."""
    assert git_fetch._GIT_TIMEOUT == 300
