"""Write-time op batching adoption on the importer (epic cold-stall-chalk / B2).

The independent import passes (Pass 1 CREATE, Pass 2a parents, Pass 2c file-impact/
verify, Pass 2d comments) are batch-committed through the ``_seam.batch_sink``
contextvar + ``event_append.batch_stage_and_commit``; Pass 2b (links) and Pass 2e
(statuses) stay per-event. These tests pin:

- **Guardrail 1** — interactive single writes are NEVER batched: a normal
  ``create``/``comment`` still makes exactly one commit each.
- **Commit-count reduction** — a batched bulk import makes far fewer commits than the
  ~one-per-event baseline.
- **Equivalence** — the batched import replays to the SAME logical state as a
  per-event import of the same NDJSON.
"""

from __future__ import annotations

import subprocess
from contextlib import contextmanager
from pathlib import Path

import rebar
from rebar import config
from rebar._commands import _seam


def _fresh_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _commit_count(repo: Path) -> int:
    tracker = str(config.tracker_dir(str(repo)))
    r = subprocess.run(
        ["git", "-C", tracker, "rev-list", "--count", "HEAD"], capture_output=True, text=True
    )
    return int(r.stdout.strip())


def _seed_many(repo: Path, n: int) -> None:
    """Seed *n* tickets, each with one comment (only batched passes 1 + 2d — no
    parents/links/statuses — so the per-event baseline is ~2n commits)."""
    root = str(repo)
    for i in range(n):
        tid = rebar.create_ticket("task", f"Task {i}", description="d" * 60, repo_root=root)
        rebar.comment(tid, f"note {i}", repo_root=root)


def _export_lines(repo: Path) -> list[str]:
    import io

    buf = io.StringIO()
    rebar.export_tickets(out=buf, repo_root=str(repo))
    return buf.getvalue().splitlines()


def _normalize(state: dict, id_to_src: dict[str, str]) -> dict:
    """Drop volatile per-run fields (fresh ids/timestamps/aliases) and rewrite every
    fresh-local-id reference (parent_id, deps + managed_refs targets) back to its
    stable source_id, so two imports of the same source compare equal."""
    volatile = {"ticket_id", "created_at", "updated_at", "env_id", "alias", "source_created_at"}
    out = {k: v for k, v in state.items() if k not in volatile}

    def to_src(local):
        return id_to_src.get(local, local)

    out["parent_id"] = to_src(out.get("parent_id")) if out.get("parent_id") else None
    out["comments"] = [
        {k: v for k, v in c.items() if k not in ("uuid", "timestamp", "created_at", "env_id")}
        for c in out.get("comments") or []
    ]
    # deps/managed_refs reference fresh local ids; rewrite targets to source ids.
    out["deps"] = sorted(
        (d.get("relation", ""), to_src(d.get("target_id"))) for d in out.get("deps") or []
    )
    out["managed_refs"] = sorted(
        [kind, to_src(target)] for kind, target in out.get("managed_refs") or []
    )
    return out


def _state_by_source(repo: Path) -> dict:
    """{source_id -> normalized replayed state} — stable across fresh-id imports."""
    fulls = [
        rebar.show_ticket(t["ticket_id"], repo_root=str(repo))
        for t in rebar.list_tickets(repo_root=str(repo))
    ]
    id_to_src = {f["ticket_id"]: (f.get("source_id") or f["ticket_id"]) for f in fulls}
    return {(f.get("source_id") or f["ticket_id"]): _normalize(f, id_to_src) for f in fulls}


@contextmanager
def _no_sink(buffer):
    """A passthrough that does NOT set the batch contextvar, so append_event commits
    per-event — used to import the same NDJSON without batching for comparison."""
    yield buffer


def test_interactive_writes_stay_one_commit_per_event(tmp_path: Path) -> None:
    """Guardrail 1: nothing outside the importer sets the sink, so a normal create and
    a normal comment each make exactly one commit."""
    repo = _fresh_repo(tmp_path, "interactive")
    root = str(repo)
    before = _commit_count(repo)
    tid = rebar.create_ticket("task", "Solo", description="d" * 60, repo_root=root)
    after_create = _commit_count(repo)
    rebar.comment(tid, "one note", repo_root=root)
    after_comment = _commit_count(repo)

    assert after_create - before == 1, "create must be one commit (not batched)"
    assert after_comment - after_create == 1, "comment must be one commit (not batched)"


def test_batched_import_makes_far_fewer_commits(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    n = 20
    _seed_many(src, n)
    lines = _export_lines(src)

    before = _commit_count(dst)
    meta = rebar.import_tickets(lines, repo_root=str(dst))
    delta = _commit_count(dst) - before

    assert meta["created"] == n
    assert meta["comments"] == n
    # Pass 1 (20 CREATEs) -> ceil(20/256)=1 commit; Pass 2d (20 comments) -> 1 commit.
    # Passes 2a/2c have no events here. So the batched delta is a tiny constant (<=3),
    # vs the ~2n=40 commits the per-event path would make.
    assert delta <= 3, f"expected a handful of batched commits, got {delta}"
    assert delta < n, "batched import must make far fewer commits than one-per-event"


def test_batched_import_equivalent_to_per_event(tmp_path: Path, monkeypatch) -> None:
    src = _fresh_repo(tmp_path, "src")
    _seed_many(src, 12)
    lines = _export_lines(src)

    # Batched (default).
    dst_batched = _fresh_repo(tmp_path, "batched")
    rebar.import_tickets(lines, repo_root=str(dst_batched))

    # Per-event: neutralize the sink so append_event commits one-per-event.
    dst_per_event = _fresh_repo(tmp_path, "perevent")
    monkeypatch.setattr(_seam, "batch_sink", _no_sink)
    rebar.import_tickets(lines, repo_root=str(dst_per_event))
    monkeypatch.undo()

    # Same logical state; the per-event path makes strictly more commits.
    assert _state_by_source(dst_batched) == _state_by_source(dst_per_event)
    assert _commit_count(dst_batched) < _commit_count(dst_per_event)


def test_batched_import_full_wireup_equivalent(tmp_path: Path, monkeypatch) -> None:
    """A richer source (parents, links, comments, statuses — exercising every pass,
    including the per-event 2b/2e) still replays identically batched vs per-event."""
    src = _fresh_repo(tmp_path, "src2")
    root = str(src)
    epic = rebar.create_ticket("epic", "Epic", description="e" * 60, repo_root=root)
    t1 = rebar.create_ticket("task", "T1", description="t" * 60, assignee="al", repo_root=root)
    t2 = rebar.create_ticket("task", "T2", description="t" * 60, repo_root=root)
    rebar.edit_ticket(t1, parent=epic, repo_root=root)
    rebar.edit_ticket(t2, parent=epic, repo_root=root)
    rebar.set_file_impact(t1, [{"path": "a.py", "reason": "x"}], repo_root=root)
    rebar.comment(t1, "note", repo_root=root)
    rebar.link(t1, t2, "relates_to", repo_root=root)
    rebar.transition(t1, "open", "in_progress", repo_root=root)
    rebar.transition(t2, "open", "closed", repo_root=root)
    lines = _export_lines(src)

    dst_batched = _fresh_repo(tmp_path, "b2")
    rebar.import_tickets(lines, repo_root=str(dst_batched))

    dst_per_event = _fresh_repo(tmp_path, "p2")
    monkeypatch.setattr(_seam, "batch_sink", _no_sink)
    rebar.import_tickets(lines, repo_root=str(dst_per_event))
    monkeypatch.undo()

    assert _state_by_source(dst_batched) == _state_by_source(dst_per_event)
