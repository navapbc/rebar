from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import get_args

import pytest

from rebar import types as rebar_types

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "scripts" / "gen_ticket_model_taxonomy.py"
DOC = REPO_ROOT / "docs" / "ticket-model.md"

pytestmark = pytest.mark.allow_unharnessed_subprocess(
    "generator check mode must execute the repository script against the documented tree"
)


def _load_gen():
    assert GEN.exists(), "generator script is missing"
    spec = importlib.util.spec_from_file_location("gen_ticket_model_taxonomy", GEN)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_gen(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(GEN), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def test_committed_taxonomy_block_lists_every_live_type_and_status() -> None:
    """The checked-in documentation mirrors the canonical literals exactly."""
    doc = DOC.read_text(encoding="utf-8")

    assert "<!-- BEGIN GENERATED TICKET TAXONOMY -->" in doc
    assert "<!-- END GENERATED TICKET TAXONOMY -->" in doc
    for ticket_type in get_args(rebar_types.TicketType):
        assert f"`{ticket_type}`" in doc
    for status in get_args(rebar_types.TicketStatus):
        assert f"`{status}`" in doc
    assert "one of five types" not in doc
    assert "five, pre-work status" not in doc


def test_check_mode_passes_for_committed_documentation() -> None:
    cp = _run_gen("--check")
    assert cp.returncode == 0, (
        f"taxonomy drift check failed:\nSTDOUT:\n{cp.stdout}\nSTDERR:\n{cp.stderr}"
    )


def test_write_mode_is_idempotent_and_preserves_relationship_prose(tmp_path: Path) -> None:
    mod = _load_gen()
    sandbox_doc = tmp_path / "ticket-model.md"
    shutil.copyfile(DOC, sandbox_doc)

    before = sandbox_doc.read_text(encoding="utf-8")
    outside_before = mod.outside_generated_taxonomy(before)
    assert mod.main(["--doc", str(sandbox_doc)]) == 0
    first = sandbox_doc.read_text(encoding="utf-8")
    assert mod.outside_generated_taxonomy(first) == outside_before

    assert mod.main(["--doc", str(sandbox_doc), "--check"]) == 0
    assert mod.main(["--doc", str(sandbox_doc)]) == 0
    assert sandbox_doc.read_text(encoding="utf-8") == first


def test_check_mode_fails_for_deliberately_stale_generated_block(tmp_path: Path) -> None:
    mod = _load_gen()
    sandbox_doc = tmp_path / "ticket-model.md"
    shutil.copyfile(DOC, sandbox_doc)
    assert mod.main(["--doc", str(sandbox_doc)]) == 0

    stale = sandbox_doc.read_text(encoding="utf-8").replace("`identity`", "`legacy_identity`", 1)
    sandbox_doc.write_text(stale, encoding="utf-8")

    cp = _run_gen("--doc", str(sandbox_doc), "--check")
    assert cp.returncode == 1
    assert "ticket taxonomy is stale" in (cp.stdout + cp.stderr)


def test_check_mode_fails_when_source_vocabulary_changes_without_regeneration(
    tmp_path: Path,
) -> None:
    mod = _load_gen()
    sandbox_doc = tmp_path / "ticket-model.md"
    sandbox_types = tmp_path / "types_with_extra_status.py"
    shutil.copyfile(DOC, sandbox_doc)
    assert mod.main(["--doc", str(sandbox_doc)]) == 0

    source = (REPO_ROOT / "src" / "rebar" / "types.py").read_text(encoding="utf-8")
    current_status_literal = (
        'TicketStatus = Literal["idea", "open", "in_progress", "blocked", "closed", '
        '"archived", "deleted"]'
    )
    drifted_status_literal = (
        'TicketStatus = Literal["idea", "open", "in_progress", "blocked", "closed", '
        '"archived", "deleted", "triaged"]'
    )
    sandbox_types.write_text(
        source.replace(current_status_literal, drifted_status_literal),
        encoding="utf-8",
    )

    cp = _run_gen("--doc", str(sandbox_doc), "--types", str(sandbox_types), "--check")
    assert cp.returncode == 1
    combined = cp.stdout + cp.stderr
    assert "ticket taxonomy is stale" in combined
    assert "triaged" in combined
