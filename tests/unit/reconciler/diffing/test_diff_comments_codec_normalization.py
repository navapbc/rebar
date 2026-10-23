"""The outbound comment dedup key must match the rich-text codec's send/round-trip form.
These tests use a non-identity AdfCodec transform that earlier
symmetric-normalization fixtures could not detect."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "outbound_differ.py"


@pytest.fixture(scope="module")
def differ() -> ModuleType:
    return importlib.import_module("rebar.reconciler.outbound_differ")


@pytest.fixture(scope="module")
def adf_codec():
    """The real Cloud ``RichTextCodec``: a genuinely non-identity outbound
    transform (soft-wrap rejoin), not a synthetic test double."""
    from rebar.reconciler.adapters.jira.rich_text_codec import AdfCodec

    return AdfCodec()


def _jira_snapshot_with_comments(jira_key: str, comment_bodies: list[str]) -> dict:
    """Jira REST API shape: fields["comment"]["comments"] (outer key "comment")."""
    jira_comments = [{"id": str(100 + i), "body": b} for i, b in enumerate(comment_bodies)]
    return {jira_key: {"comment": {"comments": jira_comments, "total": len(jira_comments)}}}


def _ticket_with_comments(comment_bodies: list[str]) -> dict:
    return {"comments": [{"body": b} for b in comment_bodies]}


# ---------------------------------------------------------------------------
# The missing cell: a non-identity outbound codec must not re-emit a comment
# that Jira already holds in its POST-TRANSFORM form.
# ---------------------------------------------------------------------------


def test_already_mirrored_comment_under_non_identity_codec_emits_no_add(differ, adf_codec) -> None:
    """A local comment whose body Jira already stores in NORMALIZED form (the
    shape the send path would actually produce through a non-identity codec)
    must emit ZERO adds — not one, as the un-normalized key produces today.

    Fixture: soft-wrapped local prose. ``AdfCodec.normalize_outbound`` rejoins
    soft wraps (delegates to ``adf.normalize_description``), so the body Jira
    would actually store after a real send is the REJOINED form, not the
    local raw form. Without routing the local key through the codec's
    ``normalize_outbound``, the raw (un-rejoined) local body never matches
    the rejoined Jira body, and the differ re-emits the comment forever —
    the exact bug-4292/DIG-5301 shape, for a transform instead of truncation.
    """
    local_raw = "first line of prose\nsoft-wrapped continuation"
    jira_stored = adf_codec.normalize_outbound(local_raw)
    assert jira_stored != local_raw, "fixture must exercise a real, non-identity transform"

    jira_key = "DIG-A32A-1"
    ticket = _ticket_with_comments([local_raw])
    snapshot = _jira_snapshot_with_comments(jira_key, [jira_stored])

    out = differ._diff_comments(ticket, jira_key, snapshot, codec=adf_codec)

    assert out == [], (
        "already-mirrored comment must not be re-emitted once the local key is "
        f"routed through the codec's normalize_outbound; got mutations: {out!r}"
    )


def test_new_comment_under_non_identity_codec_is_still_emitted(differ, adf_codec) -> None:
    """Counter-regression: a codec-aware key must not become so LOOSE that a
    genuinely new local comment silently stops being emitted."""
    jira_key = "DIG-A32A-2"
    existing_local = "first line of prose\nsoft-wrapped continuation"
    existing_jira_stored = adf_codec.normalize_outbound(existing_local)
    new_comment = "a brand new comment nobody has seen yet"

    ticket = _ticket_with_comments([existing_local, new_comment])
    snapshot = _jira_snapshot_with_comments(jira_key, [existing_jira_stored])

    out = differ._diff_comments(ticket, jira_key, snapshot, codec=adf_codec)

    assert len(out) == 1, f"expected exactly 1 add for the genuinely new comment, got {out!r}"
    assert new_comment in out[0]["body"]
    assert out[0]["action"] == "add"


def test_without_an_injected_codec_todays_baseline_behaviour_is_unchanged(
    differ, adf_codec
) -> None:
    """Without an injected codec, the default Cloud AdfCodec must normalize a sent soft
    wrap to the Jira-stored form and emit no duplicate comment."""
    jira_key = "DIG-A32A-3"
    local_raw = "first line of prose\nsoft-wrapped continuation"
    jira_stored = adf_codec.normalize_outbound(local_raw)
    assert jira_stored != local_raw, "fixture must exercise a real, non-identity transform"

    ticket = _ticket_with_comments([local_raw])
    snapshot = _jira_snapshot_with_comments(jira_key, [jira_stored])

    # No ``codec`` argument: the flipped default resolves to the real AdfCodec.
    out = differ._diff_comments(ticket, jira_key, snapshot)

    assert out == []


# ---------------------------------------------------------------------------
# Idempotence property over a small corpus of realistic bodies
# ---------------------------------------------------------------------------

_REALISTIC_BODIES = [
    pytest.param("a single short line", id="short-line"),
    pytest.param(
        "This is a soft-wrapped paragraph that a human\n"
        "would have typed across several lines in their\n"
        "editor before it gets sent to Jira.",
        id="soft-wrapped-prose",
    ),
    pytest.param(
        "```python\ndef f(x):\n    return x * 2\n```",
        id="code-fence",
    ),
    pytest.param(
        "Steps:\n- first item\n- second item\n* third item (asterisk bullet)",
        id="bullet-list",
    ),
    pytest.param("This is **bold** and this is *emphasis* too.", id="bold-and-emphasis"),
    pytest.param("A pipe table cell looks like `a | b | c` in prose.", id="literal-pipe-backtick"),
]


@pytest.mark.parametrize("body", _REALISTIC_BODIES)
def test_codec_aware_key_is_idempotent_over_realistic_bodies(differ, adf_codec, body: str) -> None:
    """Encode-then-decode is a fixed point for the comment path: once a local
    comment has been mirrored (Jira stores its normalized form), re-running
    the diff against that SAME normalized form must never re-emit — for a
    range of realistic bodies, not just one hand-picked example.
    """
    # A conventional issue-key shape. The earlier fixture value was a long uppercase
    # hyphenated string, which gitleaks scored as a generic-api-key false positive.
    jira_key = "DIG-4242"
    jira_stored = adf_codec.normalize_outbound(body)

    ticket = _ticket_with_comments([body])
    snapshot = _jira_snapshot_with_comments(jira_key, [jira_stored])

    out = differ._diff_comments(ticket, jira_key, snapshot, codec=adf_codec)

    assert out == [], (
        f"body {body!r} failed to converge: normalized form {jira_stored!r} did not "
        f"dedup against the raw local body; got mutations: {out!r}"
    )

    # And applying the codec's normalize_outbound to its OWN output changes nothing
    # further (idempotence of the transform itself, which the dedup key relies on).
    assert adf_codec.normalize_outbound(jira_stored) == jira_stored
