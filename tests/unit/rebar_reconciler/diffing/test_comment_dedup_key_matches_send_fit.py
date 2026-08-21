"""Bug e339-9709-15fe-419a: the Cloud comment dedup key must apply the SAME fit
the Cloud send path applies.

``comment_limits.py``'s module docstring states the invariant as a hard
requirement: "If the two paths used different truncation logic they could never
agree on the landed body, and the loop would persist. They MUST therefore share
this one helper so they cannot drift." Commit ``27b868ba55`` broke it on Cloud:
``acli_cli_ops.add_comment`` moved to ``AdfCodec.fit_outbound`` (which measures
the SERIALIZED ADF against 32,000) while ``_JiraSanitizer.fit_comment`` stayed on
``comment_limits.truncate_comment_body`` (a PLAIN 32,767-character cap). Bug 5931
then added a second component: ``fit_preserving_marker`` reserves budget for
``RECONCILER_MARKER``, which the differ key does not model either.

The consequence is not cosmetic. The body-equality skip is the SECONDARY dedup
layer guarding a lost comment-id map write (a crash between ``add_comment`` and
``record_comment_id``); for any over-length comment it can never fire, so such a
comment re-posts on every pass. That is the shape of failure that put 836
unmarked duplicates into live Jira.

Data Center already has the shape this file demands of Cloud:
``test_dc_backend_characterization.py::test_dc_fit_comment_is_the_wiki_codec_fit``
and ``test_dc_comment_ceiling_049e.py::test_fit_comment_converges_with_sanitize_comment``
both pin ``fit_comment`` byte-identical to what DC's send path lands. Cloud has no
such pin — this file is it.

Everything asserted here is an observable output (a returned string, a returned
mutation list); nothing reads a private name or greps source, so a
behaviour-preserving refactor cannot turn these red.
"""

from __future__ import annotations

from typing import Any

import pytest

from rebar_reconciler import outbound_comments as oc
from rebar_reconciler.adapters.jira.backend import _JiraInbound, _JiraSanitizer
from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec

# Long enough that the ADF serialization crosses 32,000 AND the plain text
# crosses 32,767, so BOTH fitters are engaged and their disagreement is visible.
_OVER_LENGTH_BODY = "Line of prose about the reconciler. " * 1200
# Over the SERIALIZED-ADF budget but UNDER the plain 32,767 cap: here the old key
# applied no truncation at all while the send path truncated. This is the window
# the headline number (32767 vs 32000) hides.
_ADF_ONLY_OVER_BODY = "z" * 32200


def _landed_text(body: str, codec: AdfCodec) -> str:
    """The exact text ``acli_cli_ops.add_comment`` hands to ``to_wire``.

    Mirrors that call site (``fit_preserving_marker(body, codec.fit_outbound)``
    over an already-decorated body) rather than restating any limit.
    """
    return oc.fit_preserving_marker(oc._decorate_outbound_comment(body), codec.fit_outbound)


def _landed_key(body: str, codec: AdfCodec) -> str:
    """What the NEXT pass reads back out of Jira for a comment we just sent.

    ``to_wire`` is the wire shape ACLI transmits and Jira stores;
    ``_normalize_comment_body`` is the differ's own Jira-side decode.
    """
    return oc._normalize_comment_body(codec.to_wire(_landed_text(body, codec)), _JiraInbound())


@pytest.fixture
def plain_cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``reconciler.rich_text_cutover`` to its shipped default.

    Set through the canonical env override rather than a stub config object: the
    comment differ resolves its vendor adapter from the same config, so a
    stand-in carrying only this flag makes unrelated machinery raise.
    """
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", "off")


@pytest.mark.parametrize("rich", [False, True], ids=["plain", "rich"])
@pytest.mark.parametrize(
    "body", [_OVER_LENGTH_BODY, _ADF_ONLY_OVER_BODY], ids=["over-both", "over-adf-only"]
)
def test_fit_comment_is_the_text_the_send_path_lands(body: str, rich: bool) -> None:
    """AC 1 — the differ's fit and the Cloud send path's fit are the identical fit.

    Asserted mode-independently: whatever ``rich_text_cutover`` is set to, the two
    must agree, because they are supposed to BE the same transform.
    """
    codec = AdfCodec(rich=rich)
    expected = _landed_text(body, codec)
    # The marker is re-appended by the send path's fit; the differ's key is
    # marker-free (the Jira-side key has it stripped), so compare the content.
    assert expected.endswith(oc.RECONCILER_MARKER), "fixture precondition: send path is decorated"
    separator = len(oc._decorate_outbound_comment("")) - len(oc.RECONCILER_MARKER)
    expected_content = expected[: len(expected) - len(oc.RECONCILER_MARKER) - separator]

    assert _JiraSanitizer().fit_comment(body) == expected_content


def test_the_two_fits_really_do_disagree_today() -> None:
    """Guards the guard: the fixture must actually engage both fitters.

    Without this, a body short enough for neither fitter would make the assertion
    above pass vacuously.
    """
    codec = AdfCodec(rich=False)
    assert _landed_text(_OVER_LENGTH_BODY, codec) != oc._decorate_outbound_comment(
        _OVER_LENGTH_BODY
    ), "fixture precondition: the send path must truncate this body"


def _snapshot(jira_key: str, bodies: list[str]) -> dict[str, Any]:
    """Jira REST shape: fields["comment"]["comments"] (outer key "comment")."""
    comments = [{"id": str(100 + i), "body": b} for i, b in enumerate(bodies)]
    return {jira_key: {"comment": {"comments": comments, "total": len(comments)}}}


def test_an_already_landed_over_length_comment_is_not_re_posted(plain_cutover: None) -> None:
    """AC 2 — the landed body equals the comparison key, so the differ emits nothing.

    The end-to-end statement of the bug: send an over-length comment, read back
    what Jira now holds, and ask the differ again. It must recognise its own
    landed body and stay silent. Driven with NO ``binding_store``, so the PRIMARY
    id-identity skip (repaired under aa7b) cannot mask the SECONDARY body-equality
    layer this ticket is about.
    """
    codec = AdfCodec(rich=False)
    landed = _landed_key(_OVER_LENGTH_BODY, codec)
    ticket = {"comments": [{"body": _OVER_LENGTH_BODY, "timestamp": "1-a"}]}

    out = oc._diff_comments(ticket, "REB-1", _snapshot("REB-1", [landed]), codec=codec)

    assert out == [], f"re-posted an already-landed comment; emitted {len(out)} mutation(s)"


def test_a_genuinely_new_over_length_comment_is_still_emitted(plain_cutover: None) -> None:
    """Counter-regression: a key made too LOOSE would swallow real new comments."""
    codec = AdfCodec(rich=False)
    landed = _landed_key(_OVER_LENGTH_BODY, codec)
    ticket = {"comments": [{"body": "a genuinely different comment", "timestamp": "2-b"}]}

    out = oc._diff_comments(ticket, "REB-1", _snapshot("REB-1", [landed]), codec=codec)

    assert len(out) == 1
    assert out[0]["action"] == "add"


def test_an_in_limit_comment_key_is_unchanged_by_this_fix(plain_cutover: None) -> None:
    """Blast-radius pin: the fix must not shift the key for a WITHIN-limit body.

    A shifted in-limit key would stop matching already-landed Jira bodies and
    re-post them — the exact failure this whole area exists to prevent.
    """
    body = "a perfectly ordinary comment\nwith two lines"
    assert _JiraSanitizer().fit_comment(body) == body
