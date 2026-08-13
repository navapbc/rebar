"""Story 3388 — the rich-text cutover must not disturb the COMMENT dedup key.

The cutover redefines ``WikiTextCodec.normalize_outbound`` in rich mode: it is
DERIVED from ``to_wire``, so for Data Center it now renders Markdown to wiki
markup instead of being the identity. That operation is shared. Descriptions want
the new behaviour; the comment path emphatically does not.

``outbound_comments._diff_comments`` builds its LOCAL comparison key by routing
the body through the injected codec's ``normalize_outbound`` (ticket a32a). If
the cutover reached that seam, every already-mirrored comment's key would shift
from the raw body to its RENDERED form while Jira still holds the plain text —
so every local comment would read as absent, and the differ would re-post the
project's entire comment history on the next pass. That failure is loud,
irreversible from Jira's side, and would land the moment an operator flipped a
flag that says nothing about comments.

Comment rendering belongs to story ``emersed-specific-mutt`` (789c); until then
the comment path stays on the plain key. These pin that, from both directions:
the flag does not reach the resolved codec, and the resulting diff is identical
with the flag off and on.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)
OUTBOUND_COMMENTS_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_comments.py"
)

pytestmark = pytest.mark.unit

# Markdown whose RENDERED wiki form is unmistakably different, so a key that
# accidentally picked up the cutover cannot coincidentally still match.
_MARKDOWN_COMMENT = "# Heading\n\n- alpha\n- beta\n"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ() -> ModuleType:
    return _load_module("outbound_differ_cutover_neutral", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def comments() -> ModuleType:
    return _load_module("outbound_comments_cutover_neutral", OUTBOUND_COMMENTS_PATH)


@pytest.fixture
def set_flag(monkeypatch: pytest.MonkeyPatch):
    """Set ``reconciler.rich_text_cutover`` through its canonical env override.

    Deliberately NOT a stubbed config object: the comment differ also resolves
    the vendor adapter and its transport from the same config, so a stand-in
    carrying only this flag makes unrelated machinery raise — a failure that
    reads like a product defect. The env override exercises the real resolution
    path end to end.
    """

    def _set(value: str) -> None:
        monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", value)

    return _set


def _jira_snapshot_with_comments(jira_key: str, bodies: list[str]) -> dict[str, Any]:
    """Jira REST API shape: fields["comment"]["comments"] (outer key "comment")."""
    jira_comments = [{"id": str(100 + i), "body": b} for i, b in enumerate(bodies)]
    return {jira_key: {"comment": {"comments": jira_comments, "total": len(jira_comments)}}}


def _ticket_with_comments(bodies: list[str]) -> dict[str, Any]:
    return {"comments": [{"body": b} for b in bodies]}


def test_the_rendered_form_really_does_differ(set_flag) -> None:
    """Guards the guard: without this the tests below could pass vacuously.

    If the DC codec's rich ``normalize_outbound`` happened to return its input,
    "the key did not shift" would be true for the wrong reason and the pin would
    protect nothing.
    """
    set_flag("both")
    from rebar_reconciler.adapters.jira_family.rich_text import WikiTextCodec

    assert WikiTextCodec(rich=True).normalize_outbound(_MARKDOWN_COMMENT) != _MARKDOWN_COMMENT


def test_the_comment_codec_stays_the_identity_with_the_flag_on(comments, set_flag) -> None:
    """The flag must not reach the comment path's resolved codec at all.

    Asserted on the RESOLVER rather than only on outcomes, because that is where
    a future change would wire the cutover in — the diff-level assertions below
    would then fail too, but this says exactly which seam moved.
    """
    set_flag("both")
    resolved = comments._resolve_codec(None)
    assert isinstance(resolved, comments._IdentityCodec)
    assert resolved.normalize_outbound(_MARKDOWN_COMMENT) == _MARKDOWN_COMMENT


@pytest.mark.parametrize("flag", ["off", "cloud", "dc", "both"])
def test_an_already_mirrored_markdown_comment_is_never_re_posted(
    differ, set_flag, flag: str
) -> None:
    """The consequence that matters: no duplicate re-post, at any flag setting.

    Jira holds the comment as the plain Markdown rebar sent. If the cutover
    reached the dedup key the local side would become rendered wiki, match
    nothing, and re-post the whole history.
    """
    set_flag(flag)
    jira_key = "DIG-3388-1"
    ticket = _ticket_with_comments([_MARKDOWN_COMMENT])
    snapshot = _jira_snapshot_with_comments(jira_key, [_MARKDOWN_COMMENT])

    assert differ._diff_comments(ticket, jira_key, snapshot) == []


def test_the_comment_diff_is_identical_with_the_flag_off_and_on(differ, set_flag) -> None:
    """Same inputs, both flag states, same mutations — including the NEW comment.

    Comparing a mixed case (one already mirrored, one genuinely new) rather than
    only the suppressed one, so the pin also catches a cutover that shifted the
    key in a way that still emitted something, just something different.
    """
    jira_key = "DIG-3388-2"
    ticket = _ticket_with_comments([_MARKDOWN_COMMENT, "## Second\n\nbrand new\n"])
    snapshot = _jira_snapshot_with_comments(jira_key, [_MARKDOWN_COMMENT])

    set_flag("off")
    before = differ._diff_comments(ticket, jira_key, snapshot)
    set_flag("both")
    after = differ._diff_comments(ticket, jira_key, snapshot)

    assert before == after
    assert len(after) == 1, f"exactly the one genuinely new comment should emit: {after!r}"
