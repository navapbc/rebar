"""WS-F1: git-canonical prompt resolution (rebar.llm.prompts).

The committed prompt file is the source of truth; Langfuse is never consulted for
text; rendering is strict; the content hash is returned for trace embedding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm import prompts
from rebar.llm.prompts import PromptError


def _reviewer():
    return prompts.get_reviewer("ticket-quality")


def test_resolve_is_git_canonical_with_content_hash() -> None:
    text, meta = prompts.resolve_prompt(_reviewer(), {"ticket_id": "T1", "ticket_context": "CTX"})
    assert "T1" in text and "CTX" in text
    assert meta["source"] == "git"
    assert meta["prompt_id"] == "ticket-quality"
    assert len(meta["content_sha256"]) == 64


def test_strict_rendering_throws_on_undefined_var() -> None:
    # ticket-quality uses {{ticket_id}} + {{ticket_context}}; omit one → raise.
    with pytest.raises(PromptError, match="undefined variable"):
        prompts.resolve_prompt(_reviewer(), {"ticket_id": "T1"})


def test_no_silent_empty_vars() -> None:
    # Supplying an unrelated var doesn't satisfy a referenced one.
    with pytest.raises(PromptError):
        prompts.resolve_prompt(_reviewer(), {"unrelated": "x"})


def test_content_hash_stable_and_matches_packaged() -> None:
    rv = _reviewer()
    text = prompts.canonical_prompt_text(rv)
    assert prompts.prompt_content_hash(text) == prompts.prompt_content_hash(text)
    assert (
        prompts.prompt_content_hash(text)
        == prompts.resolve_prompt(rv, {"ticket_id": "x", "ticket_context": "y"})[1][
            "content_sha256"
        ]
    )


def test_user_override_wins(tmp_path: Path) -> None:
    # A user .rebar/prompts/<id>.md overrides the packaged canon.
    pdir = tmp_path / ".rebar" / "prompts"
    pdir.mkdir(parents=True)
    (pdir / "ticket-quality.md").write_text("OVERRIDE {{ticket_id}}")
    text = prompts.canonical_prompt_text(_reviewer(), repo_root=str(tmp_path))
    assert text == "OVERRIDE {{ticket_id}}"
    # And resolve renders it strictly.
    out, meta = prompts.resolve_prompt(_reviewer(), {"ticket_id": "Z"}, repo_root=str(tmp_path))
    assert out == "OVERRIDE Z"


def test_template_variables_helper() -> None:
    assert prompts.template_variables("a {{x}} b {{ y }} {{x}}") == {"x", "y"}


def test_langfuse_never_imported_for_text(monkeypatch) -> None:
    # Even with a langfuse_cfg that reports enabled, no Langfuse call is made: the
    # text is git-canonical. (We assert by passing a cfg whose .enabled is True and
    # confirming resolution still succeeds offline with source=git.)
    class _Cfg:
        enabled = True

    _, meta = prompts.resolve_prompt(_reviewer(), {"ticket_id": "T", "ticket_context": "C"}, _Cfg())
    assert meta["source"] == "git"
