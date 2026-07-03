"""Tier D write-core concurrency gates (docs/bash-migration.md §6).

The headline gate is the **stiff-mop-lane** mixed-impl writer storm: N concurrent
writers on ONE clone, split across the bash leaf-write forced onto the mkdir lock
(``REBAR_WRITE_CORE=bash REBAR_FORCE_MKDIR_LOCK=1``) and the Python core
(``REBAR_WRITE_CORE=python`` — fcntl + mkdir dual leg). Before the dual leg, a
bash-mkdir writer and a python-fcntl writer did NOT mutually exclude on a
flock(1)-less host, so their concurrent ``git add``/``commit`` could collide on
``index.lock`` and lose events. With the unified lock every writer takes BOTH
mechanisms, so all N events must land and ``fsck`` stays clean.

These drive the live editable ``rebar`` (the published-vs-working-tree note: the
suite is skipped unless an on-PATH ``rebar`` resolves the working tree's
``rebar._store``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

_REBAR = shutil.which("rebar")
pytestmark = pytest.mark.integration


def _clean_env(**extra: str) -> dict:
    """A subprocess env with ALL ambient ``REBAR_*`` scrubbed.

    These tests drive the CLI through git-backed stores in ``tmp_path``; an
    inherited ``REBAR_ROOT``/``REBAR_WRITE_CORE``/``REBAR_PUSH``/… from the caller's
    shell would silently steer the writers at a different store or lock mode and
    make the storm assertions meaningless. We start from a REBAR-free environment
    and add back only the knobs each test sets explicitly (plus REBAR_NO_SYNC)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("REBAR_")}
    env["REBAR_NO_SYNC"] = "1"
    env.update(extra)
    return env


def _store_build_reason() -> str | None:
    """Return None if the on-PATH ``rebar`` is a build with ``rebar._store`` wired,
    else a human reason string. Unlike a bare ``--help`` exit check, this actually
    drives a store write (``rebar init`` in a throwaway git repo) so a stale
    published install (CLI present but missing the ``rebar._store`` write core) is
    DETECTED — turning what was a false-green silent skip into a visible xfail."""
    if not _REBAR:
        return "no on-PATH rebar"
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "probe"
        repo.mkdir()
        if subprocess.run(["git", "init", "-q"], cwd=repo).returncode != 0:
            return "git init failed in probe"
        subprocess.run(
            ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, capture_output=True
        )
        res = subprocess.run(
            [_REBAR, "init"], cwd=repo, env=_clean_env(), capture_output=True, text=True
        )
        if res.returncode != 0:
            return f"on-PATH rebar lacks a working store core: {res.stderr.strip()[:120]}"
        # The store worktree must materialise — proves rebar._store actually ran.
        if not (repo / ".tickets-tracker").exists():
            return "rebar init did not materialise the store worktree"
    return None


_STORE_BUILD_REASON = _store_build_reason()


@pytest.fixture
def clone(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    subprocess.run([_REBAR, "init"], cwd=repo, env=_clean_env(), capture_output=True, check=True)
    return repo


def _create(repo: Path, ttype: str, title: str, env_extra: dict) -> str:
    out = subprocess.run(
        [_REBAR, "create", ttype, title],
        cwd=repo,
        env=_clean_env(**env_extra),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    import re

    m = re.search(r"[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}", out)
    assert m, out
    return m.group(0)


def _count_events(repo: Path, ticket_id: str, suffix: str) -> int:
    tdir = repo / ".tickets-tracker" / ticket_id
    return len(
        [p for p in tdir.iterdir() if p.name.endswith(suffix) and not p.name.startswith(".")]
    )


@pytest.mark.xfail(
    _STORE_BUILD_REASON is not None,
    reason=f"on-PATH rebar lacks a working store core ({_STORE_BUILD_REASON}); "
    "run pipx install --editable .",
    raises=Exception,
    strict=False,
)
def test_concurrent_writer_storm_no_loss(clone: Path):
    """N concurrent comments on one ticket land with ZERO loss and a clean fsck —
    the unified dual-leg (fcntl+mkdir) lock serialises every writer correctly under
    contention. (Tier D retired the bash core, so this is the durable single-impl
    descendant of the stiff-mop-lane mixed-impl gate: the mkdir leg is always taken,
    which is exactly what closed the gap.)"""
    tid = _create(clone, "task", "storm target", {})
    n = 16

    def writer(i: int):
        return subprocess.run(
            [_REBAR, "comment", tid, f"note-{i}"],
            cwd=clone,
            env=_clean_env(),
            capture_output=True,
            text=True,
        )

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(writer, range(n)))

    failures = [(r.returncode, r.stderr) for r in results if r.returncode != 0]
    assert not failures, f"writers failed: {failures}"
    # Exactly n COMMENT events committed — no lost commits under mixed-lock contention.
    assert _count_events(clone, tid, "-COMMENT.json") == n

    # fsck clean (no index.lock corruption, no missing CREATE, etc.).
    fsck = subprocess.run(
        [_REBAR, "fsck", "--output", "json"],
        cwd=clone,
        env=_clean_env(),
        capture_output=True,
        text=True,
    )
    assert json.loads(fsck.stdout).get("issue_count") == 0


@pytest.mark.xfail(
    _STORE_BUILD_REASON is not None,
    reason=f"on-PATH rebar lacks a working store core ({_STORE_BUILD_REASON})",
    raises=Exception,
    strict=False,
)
def test_claim_storm_one_winner(clone: Path):
    """A concurrent claim storm on one open ticket (python core) yields exactly ONE
    winner and (N-1) exit-10 losers — optimistic concurrency under the unified lock."""
    tid = _create(clone, "task", "claim target", {"REBAR_WRITE_CORE": "python"})
    n = 10

    def claimer(i: int):
        return subprocess.run(
            [_REBAR, "claim", tid, "--assignee", f"agent-{i}"],
            cwd=clone,
            env=_clean_env(REBAR_WRITE_CORE="python"),
            capture_output=True,
            text=True,
        )

    with ThreadPoolExecutor(max_workers=n) as ex:
        results = list(ex.map(claimer, range(n)))

    winners = [r for r in results if r.returncode == 0]
    losers = [r for r in results if r.returncode == 10]
    assert len(winners) == 1, f"expected 1 winner, got {len(winners)}"
    assert len(losers) == n - 1, f"expected {n - 1} exit-10 losers, got {len(losers)}"
