"""RED-first tests for ticket a32a: the comment dedup key must be normalized
the way the description send value is.

``outbound_comments._diff_comments`` builds its Jira-side comparison key by
decoding whatever wire shape Jira returns (``_normalize_comment_body`` ->
``inbound_mapper.normalize_rich_text``), but its LOCAL-side key only runs
through ``sanitizer.fit_comment`` (truncation) — never through a codec's
``normalize_outbound``. ``outbound_mapper.map_fields_to_remote`` protects the
description path from exactly this asymmetry by composing
``normalize_outbound(fit_outbound(value))`` so the sent value is its own
round-trip fixed point; the comment path has no equivalent.

Every existing dedup test (``test_outbound_differ_comment_dedup.py``,
``test_diff_comments_adf.py``) uses a transformation applied to BOTH sides
(ADF decode, marker strip, whitespace strip), so none of them exercise the
cell this ticket is about: a codec whose OUTBOUND transform is not the
identity. This file uses the real ``AdfCodec`` (Cloud's actual
``RichTextCodec`` implementation, whose ``normalize_outbound`` rejoins soft
wraps via ``adf.normalize_description`` — a real, non-identity transform
already shipping in this codebase) injected into ``_diff_comments`` via its
``codec`` parameter, so nothing here is a synthetic stand-in.

Convergence is the entire point (bug 4292's DIG-5301 reached 14 duplicate
comments from exactly this shape of bug), so every "no mutation expected"
assertion here is a duplicate-comment regression test, and the "genuinely
new comment" test is the counter-regression: a fix that makes the key too
LOOSE would silently swallow real new comments.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
OUTBOUND_DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "outbound_differ.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def differ() -> ModuleType:
    return _load_module("outbound_differ_codec_normalization", OUTBOUND_DIFFER_PATH)


@pytest.fixture(scope="module")
def adf_codec():
    """The real Cloud ``RichTextCodec``: a genuinely non-identity outbound
    transform (soft-wrap rejoin), not a synthetic test double."""
    from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec

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
    """emersed-specific-mutt flips the ``_resolve_codec`` default from the a32a
    ``_IdentityCodec`` to the REAL Cloud ``AdfCodec`` (the DC comment-diff
    dedup-key migration 3388 deferred to this story), so the local dedup key is
    now normalized the way the LANDED wire is — even when no caller injects a
    codec. The send path ADF-encodes, so Jira stores the NORMALIZED (soft-wrap
    rejoined) form, and the un-injected diff converges against it with zero
    mutations. (Before this story the default was identity and this asserted a
    verbatim echo; that premise — "Cloud sends plain text, no ADF" — is exactly
    what this story overturns.)
    """
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
