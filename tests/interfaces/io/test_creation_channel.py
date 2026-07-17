"""creation_channel for NDJSON import (story e622, epic jira-reb-977).

NDJSON import creates a *fresh local* ticket, so it must report
``creation_channel="import"`` — never copying the exported source record's own
channel — while the existing ``source_*`` provenance keeps describing the prior
store. Observable oracle only: the imported ticket's projected state.

``-k`` selector: import.
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
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True, capture_output=True)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _export_lines(repo: Path) -> list[str]:
    buf = io.StringIO()
    rebar.export_tickets(out=buf, repo_root=str(repo))
    return [ln for ln in buf.getvalue().splitlines() if ln.strip()]


def _by_title(repo: Path) -> dict:
    return {t["title"]: t for t in rebar.list_tickets(repo_root=str(repo))}


def test_import_records_import_channel(tmp_path: Path):
    src = _fresh_repo(tmp_path, "src")
    dst = _fresh_repo(tmp_path, "dst")
    tid = rebar.create_ticket("task", "Imported Task", repo_root=str(src))

    # The source ticket carries a python channel; export includes it.
    exported = _export_lines(src)
    rebar.import_tickets(exported, repo_root=str(dst))

    imported = _by_title(dst)["Imported Task"]
    # The new LOCAL creation is `import`, NOT a copy of the source's `python`.
    assert imported["creation_channel"] == "import"
    assert imported.get("creation_channel_inferred") is None
    # Fresh local id, but source_* provenance preserved.
    assert imported["ticket_id"] != tid
    assert imported["source_id"] == tid


def test_export_includes_creation_channel(tmp_path: Path):
    # AC4: export carries creation_channel through the TicketState output contract
    # (no parallel serializer rule) — a guard that the field survives export.
    src = _fresh_repo(tmp_path, "exp")
    rebar.create_ticket("task", "Exported Task", repo_root=str(src))
    rows = [json.loads(ln) for ln in _export_lines(src)]
    row = next(r for r in rows if r.get("title") == "Exported Task")
    assert row["creation_channel"] == "python"
