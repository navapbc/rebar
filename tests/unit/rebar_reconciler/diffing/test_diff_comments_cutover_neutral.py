"""Story 3388 → landed by emersed-specific-mutt: the comment path renders through
the flag-governed codec and stays convergent.

The rich-text cutover redefines ``normalize_outbound`` in rich mode (derived from
``to_wire``: DC renders Markdown to wiki, Cloud round-trips through ADF). Story
3388 moved DESCRIPTIONS onto that codec but DEFERRED the comment path to
``emersed-specific-mutt`` (789c) — which this file now belongs to. This story
flips the comment path onto the SAME flag-governed ``RichTextCodec`` for BOTH the
send surface (``acli`` ADF encode / DC wiki render) AND the LOCAL dedup key, so
the two move together: Jira holds the RENDERED wire, the local key is normalized
to that same form, and an already-mirrored comment is never re-posted at ANY flag
setting. These pin that migrated convergence — the codec is now the real,
flag-sensitive one (no longer the a32a ``_IdentityCodec``), and a comment whose
Jira-stored body is the codec's rendered form suppresses cleanly.
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


def test_the_comment_codec_is_the_real_flag_governed_codec(comments, set_flag) -> None:
    """emersed-specific-mutt: the comment path's resolved codec is the REAL,
    flag-governed ``RichTextCodec`` (no longer the a32a ``_IdentityCodec``).

    Asserted on the RESOLVER because that is the seam this story moved: the
    ``rich`` mode of the resolved codec tracks ``reconciler.rich_text_cutover``,
    so with the flag OFF it renders plain and with the flag ON it renders rich."""
    set_flag("off")
    off = comments._resolve_codec(None)
    assert not isinstance(off, comments._IdentityCodec)
    assert off._rich is False

    set_flag("both")
    on = comments._resolve_codec(None)
    assert not isinstance(on, comments._IdentityCodec)
    assert on._rich is True


@pytest.mark.parametrize("flag", ["off", "cloud", "dc", "both"])
def test_an_already_mirrored_markdown_comment_is_never_re_posted(
    differ, comments, set_flag, flag: str
) -> None:
    """The consequence that matters: no duplicate re-post, at any flag setting.

    Post-migration the SEND path renders, so Jira holds the comment in the codec's
    RENDERED form. The local dedup key is normalized through that SAME flag-governed
    codec, so the already-mirrored comment matches and is suppressed — the
    append-only convergence guarantee, at every flag value."""
    set_flag(flag)
    jira_key = "DIG-3388-1"
    # Jira holds what the send path would land: the codec's rendered wire.
    codec = comments._resolve_codec(None)
    jira_stored = codec.normalize_outbound(_MARKDOWN_COMMENT)
    ticket = _ticket_with_comments([_MARKDOWN_COMMENT])
    snapshot = _jira_snapshot_with_comments(jira_key, [jira_stored])

    assert differ._diff_comments(ticket, jira_key, snapshot) == []


def test_the_comment_diff_converges_at_each_flag(differ, comments, set_flag) -> None:
    """Mixed case (one already mirrored, one genuinely new) converges at BOTH flag
    states: the mirrored comment (stored in its codec-rendered form) is suppressed
    and exactly the one new comment emits — whether the flag is off or on."""
    jira_key = "DIG-3388-2"
    new_comment = "## Second\n\nbrand new\n"

    for flag in ("off", "both"):
        set_flag(flag)
        codec = comments._resolve_codec(None)
        jira_stored = codec.normalize_outbound(_MARKDOWN_COMMENT)
        ticket = _ticket_with_comments([_MARKDOWN_COMMENT, new_comment])
        snapshot = _jira_snapshot_with_comments(jira_key, [jira_stored])

        out = differ._diff_comments(ticket, jira_key, snapshot)
        assert len(out) == 1, f"flag={flag}: exactly the one new comment should emit: {out!r}"
