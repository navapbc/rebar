"""Story afe6: the unified prompt model — `get_prompt`, the derived prompt index +
its invariants (one default, no dimension collision, retire-on-regen), and the
execution_mode stamp on every migrated reviewer."""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm import prompts
from rebar.llm.prompts import PromptError, PromptNotFound


# ── get_prompt: unified resolver ──────────────────────────────────────────────
def test_get_prompt_returns_unified_reviewer() -> None:
    p = prompts.get_prompt("ticket-quality")
    assert p.id == "ticket-quality"
    assert p.is_reviewer is True  # category == "review", EXPLICIT
    assert p.category == "review"
    assert p.execution_mode == "agentic"
    assert p.dimension == "ticket-quality"
    assert "{{ticket_id}}" in p.text  # body, front-matter stripped


def test_get_prompt_project_override_wins(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "ticket-quality.md").write_text(
        "---\ncategory: review\ndimension: ticket-quality\n---\nOVERRIDE {{ticket_id}}"
    )
    p = prompts.get_prompt("ticket-quality", repo_root=str(tmp_path))
    assert p.text == "OVERRIDE {{ticket_id}}"
    assert p.is_reviewer is True
    # Without the repo_root the packaged built-in is resolved instead.
    assert "OVERRIDE" not in prompts.get_prompt("ticket-quality").text


def test_get_prompt_unknown_raises() -> None:
    with pytest.raises(PromptNotFound):
        prompts.get_prompt("no-such-prompt")


def test_non_review_category_is_not_reviewer(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "summarize.md").write_text("---\ncategory: transform\n---\nDo a thing.")
    p = prompts.get_prompt("summarize", repo_root=str(tmp_path))
    assert p.is_reviewer is False and p.category == "transform"


# ── every migrated reviewer carries execution_mode ────────────────────────────
def test_every_reviewer_has_execution_mode_stamped() -> None:
    for rid in prompts.load_catalog():
        assert prompts.get_prompt(rid).execution_mode == "agentic", rid


# ── derived index + invariants ────────────────────────────────────────────────
def test_index_on_disk_matches_regenerated() -> None:
    import json

    built = prompts.build_prompt_index()
    on_disk = json.loads(prompts._index_path().read_text(encoding="utf-8"))
    assert on_disk == built  # committed index is not stale


def test_index_has_exactly_one_default() -> None:
    cat = prompts.load_catalog()
    assert sum(1 for r in cat.values() if r.default) == 1


def _write_prompt(d: Path, name: str, meta_lines: str, body: str = "BODY") -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(f"---\n{meta_lines}\n---\n{body}")


def _scan_dir(monkeypatch, d: Path) -> None:
    """Point the prompt-index scanner at a fixture dir of *.md prompts."""
    from importlib.resources import files as _files  # noqa: F401

    monkeypatch.setattr(prompts, "_catalog_dir", lambda: d)


def test_index_rejects_two_defaults(monkeypatch, tmp_path: Path) -> None:
    d = tmp_path / "reviewers"
    _write_prompt(d, "a.md", "category: review\ndimension: da\ndefault: true")
    _write_prompt(d, "b.md", "category: review\ndimension: db\ndefault: true")
    _scan_dir(monkeypatch, d)
    with pytest.raises(PromptError, match="EXACTLY ONE default"):
        prompts.build_prompt_index()


def test_index_rejects_dimension_collision(monkeypatch, tmp_path: Path) -> None:
    d = tmp_path / "reviewers"
    _write_prompt(d, "a.md", "category: review\ndimension: same\ndefault: true")
    _write_prompt(d, "b.md", "category: review\ndimension: same\ndefault: false")
    _scan_dir(monkeypatch, d)
    with pytest.raises(PromptError, match="dimension"):
        prompts.build_prompt_index()


def test_index_retires_removed_prompt(monkeypatch, tmp_path: Path) -> None:
    d = tmp_path / "reviewers"
    _write_prompt(d, "keep.md", "category: review\ndimension: dk\ndefault: true")
    _write_prompt(d, "gone.md", "category: review\ndimension: dg\ndefault: false")
    _scan_dir(monkeypatch, d)
    assert set(prompts.build_prompt_index()) == {"keep", "gone"}
    (d / "gone.md").unlink()  # RETIRE: removed prompt drops out on regeneration
    assert set(prompts.build_prompt_index()) == {"keep"}


def test_non_review_prompts_excluded_from_index(monkeypatch, tmp_path: Path) -> None:
    d = tmp_path / "reviewers"
    _write_prompt(d, "rev.md", "category: review\ndimension: dr\ndefault: true")
    _write_prompt(d, "xform.md", "category: transform")
    _scan_dir(monkeypatch, d)
    assert set(prompts.build_prompt_index()) == {"rev"}  # only reviewers


def test_get_prompt_rejects_path_traversal_ids() -> None:
    # Defense-in-depth (three-pass review finding S1/S2): a prompt id becomes a path
    # component, so a traversal id must never resolve — even though the editor's read
    # endpoints are loopback+token guarded.
    for bad in ("../jira-config", "../../.env", "a/b", "x..y", "", "/etc/passwd"):
        with pytest.raises(prompts.PromptNotFound):
            prompts.get_prompt(bad, repo_root=".")
