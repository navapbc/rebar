"""Perf refactor (bug 1cc0): the verify-identity gate must resolve every event's
introducing commit in ONE `git log` pass, not one subprocess per event.

The old path called ``resolve_event_commit`` once per non-verified event, each a full-history
``git log`` — O(events × history), ~1.8h at real store scale. The batched
``build_introducing_commit_map`` walks history once (O(events + history)). These tests pin
BOTH invariants that matter:

* correctness — the batch map returns the SAME introducing commit as the per-event resolver
  for every active event (parity against the ground-truth function);
* efficiency — running the full-store gate does not fall back to the per-event resolver when
  the batch map covers every event (mirrors the build_dep_graph single-batch-scan precedent
  in tests/scripts/graph/test_graph_perf.py).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import verify_authorship
from rebar._commands._seam import tracker_dir
from rebar.attest import authorship


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=r, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _active_event_paths(tracker: str) -> list[tuple[str, str, str]]:
    """(rel_path, position, ticket_dir) for every active event file, mirroring _collect_all."""
    from rebar.reducer._cache import is_active_event

    out: list[tuple[str, str, str]] = []
    for tid in sorted(os.listdir(tracker)):
        td = os.path.join(tracker, tid)
        if tid.startswith(".") or not os.path.isdir(td):
            continue
        for fn in sorted(os.listdir(td)):
            if not fn.endswith(".json") or fn.startswith(".") or not is_active_event(fn):
                continue
            with open(os.path.join(td, fn), encoding="utf-8") as f:
                ev = json.load(f)
            if ev.get("event_type") == "SNAPSHOT":
                continue
            position = f"{ev.get('timestamp')}-{ev.get('uuid')}"
            out.append((f"{tid}/{fn}", position, td))
    return out


def _seed_multi_commit_store(repo: Path) -> str:
    """Several tickets + edits, each write a distinct tickets-branch commit."""
    a = rebar.create_ticket("task", "alpha task", repo_root=str(repo), return_alias=True)
    b = rebar.create_ticket("bug", "beta bug", repo_root=str(repo), return_alias=True)
    rebar.create_ticket("story", "gamma story", repo_root=str(repo))
    rebar.edit_ticket(a["id"], description="alpha now described", repo_root=str(repo))
    rebar.comment(b["id"], "a comment on beta", repo_root=str(repo))
    rebar.edit_ticket(b["id"], description="beta now described", repo_root=str(repo))
    rebar.tag(a["id"], "reviewed", repo_root=str(repo))
    return str(tracker_dir(str(repo)))


# ── correctness parity ────────────────────────────────────────────────────────
def test_batch_map_matches_per_event_resolver(repo: Path) -> None:
    """build_introducing_commit_map[path] == resolve_event_commit(...) for every event."""
    tracker = _seed_multi_commit_store(repo)
    events = _active_event_paths(tracker)
    assert len(events) >= 5, "fixture should produce several events across commits"

    commit_map = authorship.build_introducing_commit_map(repo_root=str(repo))

    for rel, position, td in events:
        ground_truth = authorship.resolve_event_commit(position, td, repo_root=str(repo))
        assert ground_truth is not None, f"per-event resolver found no commit for {rel}"
        assert commit_map.get(rel) == ground_truth, (
            f"batch map disagrees for {rel}: map={commit_map.get(rel)} per-event={ground_truth}"
        )


def test_batch_map_commit_actually_added_the_file(repo: Path) -> None:
    """Each mapped commit is a real commit whose diff ADDS that path (not just any touch)."""
    tracker = _seed_multi_commit_store(repo)
    commit_map = authorship.build_introducing_commit_map(repo_root=str(repo))
    assert commit_map
    for rel, sha in commit_map.items():
        names = subprocess.run(
            ["git", "-C", tracker, "show", "--diff-filter=A", "--name-only", "--format=", sha],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        assert rel in names, f"{sha} does not add {rel}"


# ── efficiency invariant (mirrors build_dep_graph single-batch-scan) ───────────
def test_gate_does_not_call_per_event_resolver(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A full-store scan resolves commits from the batch map, NOT one git-log per event.

    With every active event covered by build_introducing_commit_map, the per-event
    resolve_event_commit must not be invoked at all — the O(events × history) behaviour is
    gone. Kept RED until verify_authorship builds the map once and looks up per event.
    """
    _seed_multi_commit_store(repo)

    calls: list[tuple[str, str]] = []
    real = authorship.resolve_event_commit

    def counting(position, ticket_dir, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((position, ticket_dir))
        return real(position, ticket_dir, **kwargs)

    monkeypatch.setattr(authorship, "resolve_event_commit", counting)

    rc = verify_authorship.cli(["--all", "--root", str(repo)])
    assert rc == 0
    assert calls == [], f"expected 0 per-event resolver calls, got {len(calls)}: {calls[:5]}"


# ── fail-closed safety (a merge-gate must degrade, never crash or skip) ────────
def test_empty_map_falls_back_to_per_event_resolver(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the batch map is empty (git failure / merge-introduced path), the gate still resolves
    each commit via the per-event resolver — fail-closed, not a silent skip. Enforcement here
    (require_authenticated) must still fail on the unsigned events, exactly as before."""
    _seed_multi_commit_store(repo)

    monkeypatch.setattr(authorship, "build_introducing_commit_map", lambda **kw: {})
    calls: list[str] = []
    real = authorship.resolve_event_commit

    def counting(position, ticket_dir, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(position)
        return real(position, ticket_dir, **kwargs)

    monkeypatch.setattr(authorship, "resolve_event_commit", counting)

    rc = verify_authorship.cli(["--all", "--require-authenticated", "--root", str(repo)])
    assert rc != 0, "unsigned events under enforcement must fail the gate"
    assert calls, "empty map must fall back to the per-event resolver, not skip resolution"


def test_build_map_never_raises_on_non_git_dir(tmp_path: Path) -> None:
    """build_introducing_commit_map is fail-closed: a non-git / missing tracker yields {}."""
    assert authorship.build_introducing_commit_map(repo_root=str(tmp_path / "nope")) == {}
