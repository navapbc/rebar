"""Story B-DM: the prompt + criteria library WRITE + structured-ENUMERATE data model
(:mod:`rebar.llm.prompt_library`) — the backend the visual editor sits on.

Covers: enumerate shape/fields for packaged + user-dir entries (incl. criteria +
overlay flag), create/update round-trip (the new id resolves via get_prompt and
enumerate), the committed packaged index staying consistent after a USER write (the
drift-gate contract), and the validation paths (id collision, malformed/missing
front-matter, invalid/reserved id). Pure stdlib + PyYAML; writes only under
``tmp_path`` (never the packaged dir), so the committed index is never dirtied."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.llm import prompts
from rebar.llm.plan_review import registry
from rebar.llm.prompt_library import (
    InvalidPromptIdError,
    LibraryWriteError,
    PromptExistsError,
    PromptNotFound,
    create_prompt,
    enumerate_criteria,
    enumerate_library,
    update_prompt,
)

_PROMPT_MD = (
    "---\ntitle: My Prompt\ndescription: A user-authored prompt.\n"
    "category: transform\n---\nDo a thing with {{x}}.\n"
)
_CRITERION_MD = (
    "---\ntitle: Custom overlay criterion\ndescription: A user criterion.\n"
    "category: plan-review-criterion\nexecution_mode: agentic\n---\nJudge the thing.\n"
)


# ── enumerate ───────────────────────────────────────────────────────────────────


def test_enumerate_packaged_includes_reviewers_and_criteria() -> None:
    entries = enumerate_library()
    by_id = {e["id"]: e for e in entries}
    # a packaged reviewer
    assert by_id["ticket-quality"]["kind"] == "prompt"
    assert by_id["ticket-quality"]["source"] == "packaged"
    assert by_id["ticket-quality"]["is_overlay"] is False
    # a packaged criterion (category: plan-review-criterion)
    g6 = by_id["plan-review-G6"]
    assert g6["kind"] == "criterion"
    assert g6["is_overlay"] is False  # G6 is not a Txx overlay
    # an overlay criterion is flagged via the registry
    assert by_id["plan-review-T1"]["kind"] == "criterion"
    assert by_id["plan-review-T1"]["is_overlay"] is True
    assert registry.is_overlay("T1") is True
    # every entry carries the full picker shape
    for e in entries:
        assert set(e) == {
            "id",
            "kind",
            "title",
            "description",
            "inputs",
            "outputs",
            "is_overlay",
            "source",
        }


def test_enumerate_covers_user_dir_entries(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "my-prompt.md").write_text(_PROMPT_MD, encoding="utf-8")
    by_id = {e["id"]: e for e in enumerate_library(repo_root=str(tmp_path))}
    assert by_id["my-prompt"]["source"] == "user"
    assert by_id["my-prompt"]["kind"] == "prompt"
    assert by_id["my-prompt"]["description"] == "A user-authored prompt."
    # without repo_root, the user entry is absent (packaged-only)
    assert "my-prompt" not in {e["id"] for e in enumerate_library()}


def test_enumerate_user_override_wins_over_packaged(tmp_path: Path) -> None:
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "ticket-quality.md").write_text(
        "---\ntitle: Overridden\ndescription: my override\ncategory: review\n"
        "---\nbody {{ticket_id}}\n",
        encoding="utf-8",
    )
    by_id = {e["id"]: e for e in enumerate_library(repo_root=str(tmp_path))}
    assert by_id["ticket-quality"]["source"] == "user"
    assert by_id["ticket-quality"]["title"] == "Overridden"


def test_enumerate_criteria_is_criteria_only() -> None:
    crits = enumerate_criteria()
    assert crits, "expected packaged criteria"
    assert all(e["kind"] == "criterion" for e in crits)
    assert "plan-review-G6" in {e["id"] for e in crits}
    assert "ticket-quality" not in {e["id"] for e in crits}


# ── create / update round-trip ───────────────────────────────────────────────────


def test_create_writes_and_resolves(tmp_path: Path) -> None:
    path = create_prompt("my-prompt", _PROMPT_MD, repo_root=str(tmp_path))
    assert path == tmp_path / ".rebar" / "prompts" / "my-prompt.md"
    assert path.is_file()
    # canonical form: schema_version stamped by the writer
    assert "schema_version:" in path.read_text(encoding="utf-8")
    # resolves via get_prompt and enumerate
    p = prompts.get_prompt("my-prompt", repo_root=str(tmp_path))
    assert p.text == "Do a thing with {{x}}.\n"
    assert p.description == "A user-authored prompt."
    assert "my-prompt" in {e["id"] for e in enumerate_library(repo_root=str(tmp_path))}


def test_create_criterion_resolves_via_enumerate(tmp_path: Path) -> None:
    create_prompt("plan-review-custom", _CRITERION_MD, repo_root=str(tmp_path))
    by_id = {e["id"]: e for e in enumerate_library(repo_root=str(tmp_path))}
    assert by_id["plan-review-custom"]["kind"] == "criterion"
    assert by_id["plan-review-custom"]["source"] == "user"


def test_update_modifies_existing_user_entry(tmp_path: Path) -> None:
    create_prompt("my-prompt", _PROMPT_MD, repo_root=str(tmp_path))
    updated = (
        "---\ntitle: My Prompt\ndescription: Updated text.\n"
        "category: transform\n---\nNew body {{x}}.\n"
    )
    update_prompt("my-prompt", updated, repo_root=str(tmp_path))
    p = prompts.get_prompt("my-prompt", repo_root=str(tmp_path))
    assert p.description == "Updated text."
    assert p.text == "New body {{x}}.\n"


def test_update_missing_user_entry_raises(tmp_path: Path) -> None:
    with pytest.raises(PromptNotFound):
        update_prompt("never-created", _PROMPT_MD, repo_root=str(tmp_path))


# ── the committed packaged index stays consistent after a USER write ─────────────


def test_user_write_does_not_change_committed_index(tmp_path: Path) -> None:
    # The committed packaged index is DERIVED from packaged reviewers only; a user
    # write must leave it byte-consistent so the CI drift gate stays green.
    committed = json.loads(
        (Path(prompts.__file__).parent / "reviewers" / "index.json").read_text(encoding="utf-8")
    )
    assert prompts.build_prompt_index() == committed  # baseline: index is up to date
    create_prompt("my-prompt", _PROMPT_MD, repo_root=str(tmp_path))
    create_prompt("plan-review-custom", _CRITERION_MD, repo_root=str(tmp_path))
    # regen (packaged-only, as CI runs it) is unchanged by the user-dir writes
    assert prompts.build_prompt_index() == committed


# ── validation paths ─────────────────────────────────────────────────────────────


def test_create_id_collision_raises(tmp_path: Path) -> None:
    create_prompt("my-prompt", _PROMPT_MD, repo_root=str(tmp_path))
    with pytest.raises(PromptExistsError):
        create_prompt("my-prompt", _PROMPT_MD, repo_root=str(tmp_path))


def test_create_malformed_front_matter_raises(tmp_path: Path) -> None:
    bad = "---\ntitle: : : not valid yaml :\n  - broken\n---\nbody\n"
    with pytest.raises(prompts.PromptError):
        create_prompt("bad", bad, repo_root=str(tmp_path))


def test_create_missing_front_matter_raises(tmp_path: Path) -> None:
    with pytest.raises(LibraryWriteError):
        create_prompt("nofm", "just a body, no front-matter\n", repo_root=str(tmp_path))


def test_create_missing_required_key_raises(tmp_path: Path) -> None:
    no_desc = "---\ntitle: Only a title\n---\nbody\n"
    with pytest.raises(LibraryWriteError):
        create_prompt("nodesc", no_desc, repo_root=str(tmp_path))


@pytest.mark.parametrize("bad_id", ["", "../escape", "a/b", "has space", "dot.ted", "..", "a\\b"])
def test_create_invalid_id_raises(tmp_path: Path, bad_id: str) -> None:
    with pytest.raises(InvalidPromptIdError):
        create_prompt(bad_id, _PROMPT_MD, repo_root=str(tmp_path))


def test_create_invalid_execution_mode_raises(tmp_path: Path) -> None:
    bad = "---\ntitle: T\ndescription: d\nexecution_mode: turbo\n---\nbody\n"
    with pytest.raises(LibraryWriteError):
        create_prompt("badmode", bad, repo_root=str(tmp_path))
