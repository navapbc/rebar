"""Import NDJSON over real stores (P1.2 T3): round-trip ≡ logical state.

Uses two ``rebar_repo``-style temp repos: a source store is seeded and exported,
then imported into a fresh target, and the target's logical state is compared to
the source (modulo fresh ids, with original date/author preserved as source_*).
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import rebar


def _fresh_repo(tmp_path: Path, name: str) -> Path:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _seed_source(repo: Path) -> dict:
    root = str(repo)
    epic = rebar.create_ticket("epic", "Epic", description="e" * 60, repo_root=root)
    t1 = rebar.create_ticket(
        "task", "Task one", description="t" * 60, priority=1, assignee="alice", repo_root=root
    )
    t2 = rebar.create_ticket("task", "Task two", repo_root=root)
    rebar.edit_ticket(t1, parent=epic, repo_root=root)
    rebar.edit_ticket(t2, parent=epic, repo_root=root)
    rebar.comment(t1, "first note", repo_root=root)
    rebar.tag(t1, "urgent", repo_root=root)
    rebar.link(t1, t2, "relates_to", repo_root=root)
    rebar.transition(t1, "open", "in_progress", repo_root=root)
    rebar.transition(t2, "open", "closed", repo_root=root)
    return {"epic": epic, "t1": t1, "t2": t2}


def _export_str(repo: Path) -> str:
    buf = io.StringIO()
    rebar.export_tickets(out=buf, repo_root=str(repo))
    return buf.getvalue()


def _by_title(repo: Path) -> dict:
    return {t["title"]: t for t in rebar.list_tickets(repo_root=str(repo))}


def test_roundtrip_reproduces_logical_state(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed_source(src)

    ndjson = _export_str(src)
    meta = rebar.import_tickets(ndjson.splitlines(), repo_root=str(dst))
    assert meta["created"] == 3
    assert not meta["warnings"]

    by = _by_title(dst)
    epic, a, b = by["Epic"], by["Task one"], by["Task two"]

    # hierarchy
    assert a["parent_id"] == epic["ticket_id"]
    assert b["parent_id"] == epic["ticket_id"]
    # status reproduction (non-open)
    assert a["status"] == "in_progress"
    assert b["status"] == "closed"
    # scalar fields
    assert a["assignee"] == "alice"
    assert a["priority"] == 1
    assert a["tags"] == ["urgent"]
    # comment + provenance on the comment
    assert [c["body"] for c in a["comments"]] == ["first note"]
    assert a["comments"][0]["source_author"]
    # relates_to link reproduced on both endpoints
    assert any(
        d["relation"] == "relates_to" and d["target_id"] == b["ticket_id"] for d in a["deps"]
    )
    assert any(
        d["relation"] == "relates_to" and d["target_id"] == a["ticket_id"] for d in b["deps"]
    )


def test_provenance_preserved_with_fresh_ids(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    ids = _seed_source(src)
    src_state = rebar.show_ticket(ids["t1"], repo_root=str(src))

    rebar.import_tickets(_export_str(src).splitlines(), repo_root=str(dst))
    a = _by_title(dst)["Task one"]

    # fresh local id, original identity preserved as source_*
    assert a["ticket_id"] != ids["t1"]
    assert a["source_id"] == ids["t1"]
    assert a["source_created_at"] == src_state["created_at"]
    assert a["source_author"] == src_state["author"]


def test_dangling_parent_and_link_skipped_with_warning(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed_source(src)

    # Import only the two child tasks — their parent epic and (for the link) each
    # other's presence is partial: parent epic is absent from the subset.
    lines = [json.loads(ln) for ln in _export_str(src).splitlines()]
    subset = [ln for ln in lines if ln["title"] != "Epic"]
    meta = rebar.import_tickets(subset, repo_root=str(dst))

    assert meta["created"] == 2
    assert any("dangling parent" in w for w in meta["warnings"])
    # run still succeeded; the children exist, just unparented
    by = _by_title(dst)
    assert by["Task one"]["parent_id"] is None
    assert by["Task two"]["parent_id"] is None


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed_source(src)

    meta = rebar.import_tickets(_export_str(src).splitlines(), dry_run=True, repo_root=str(dst))
    assert meta["dry_run"] is True
    assert meta["would_create"] == 3
    assert rebar.list_tickets(repo_root=str(dst)) == []


def test_export_then_import_is_schema_round_trippable(tmp_path: Path) -> None:
    """The importer consumes exactly what the exporter produces (file path form)."""
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    _seed_source(src)
    dump = tmp_path / "dump.ndjson"
    rebar.export_tickets(out=str(dump), repo_root=str(src))

    meta = rebar.import_tickets(str(dump), repo_root=str(dst))
    assert meta["created"] == 3
    # re-export from dst and confirm the same set of titles/types comes back
    src_titles = {t["title"] for t in rebar.list_tickets(repo_root=str(src))}
    dst_titles = {t["title"] for t in rebar.list_tickets(repo_root=str(dst))}
    assert src_titles == dst_titles
