"""Bug e339-9709-15fe-419a: the Cloud comment dedup key must apply the SAME fit
the Cloud send path applies.

``comment_limits.py``'s module docstring states the invariant as a hard
requirement: "If the two paths used different truncation logic they could never
agree on the landed body, and the loop would persist. They MUST therefore share
this one helper so they cannot drift." Ticket `emersed-specific-mutt` broke it on
Cloud:
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
``test_dc_backend_characterization.py::test_dc_fit_comment_is_the_send_path_fit``
and ``test_dc_comment_ceiling_049e.py::test_fit_comment_converges_with_sanitize_comment``
both pin ``fit_comment`` byte-identical to what DC's send path lands. Cloud has no
such pin — this file is it.

Everything asserted here is an observable output (a returned string, a returned
mutation list); nothing reads a private name or greps source, so a
behaviour-preserving refactor cannot turn these red.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rebar_reconciler import outbound_comments as oc
from rebar_reconciler.adapters.jira import acli_cli_ops
from rebar_reconciler.adapters.jira.backend import _JiraInbound, _JiraSanitizer
from rebar_reconciler.adapters.jira.rich_text_codec import AdfCodec

# Long enough that the ADF serialization crosses 32,000 AND the plain text
# crosses 32,767, so BOTH fitters are engaged and their disagreement is visible.
_OVER_LENGTH_BODY = "Line of prose about the reconciler. " * 1200
# Over the SERIALIZED-ADF budget but UNDER the plain 32,767 cap: here the old key
# applied no truncation at all while the send path truncated. This is the window
# the headline number (32767 vs 32000) hides.
_ADF_ONLY_OVER_BODY = "z" * 32200


def _send(body: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """DRIVE the real send path and return the ``--body`` argument it produced.

    The send side of every assertion in this file is CAPTURED from
    ``acli_cli_ops.add_comment`` — the actual production call site — rather than
    re-derived here from the helpers that call site happens to use today.

    That distinction is the whole point of this file. The test this bug replaced
    (``mutate/test_outbound_comment_length_convergence.py``) modelled the landed
    body as ``comment_limits.truncate_comment_body(emitted)``; when
    `emersed-specific-mutt` moved ``add_comment`` onto a different fitter, that went
    stale silently and the "convergence" test stayed green for another six months
    while live convergence was broken. A test that re-derives the send path
    inherits exactly that failure mode: it measures the helper it named, not the
    path that ships. Only ``_run_acli`` is stubbed, so everything between
    ``add_comment``'s signature and the wire is real, and a future change to the
    composition inside ``add_comment`` moves this side of the assertion with it.
    """
    captured: dict[str, Any] = {}

    class _Result:
        stdout = '{"id": "10001"}'

    def _fake_run(cmd: list[str], acli_cmd: Any = None) -> Any:
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(acli_cli_ops.acli_subprocess, "_run_acli", _fake_run)
    # add_comment receives an ALREADY-decorated body (the applier decorates before
    # dispatch), exactly as _diff_comments emits it.
    acli_cli_ops.add_comment("REB-1", oc._decorate_outbound_comment(body))
    cmd = captured["cmd"]
    return str(cmd[cmd.index("--body") + 1])


def _landed_key(body: str, monkeypatch: pytest.MonkeyPatch) -> str:
    """What the NEXT pass reads back out of Jira for a comment we just sent.

    The captured ``--body`` is the serialized wire form ACLI transmits and Jira
    stores; ``_normalize_comment_body`` is the differ's own Jira-side decode of it.
    """
    return oc._normalize_comment_body(json.loads(_send(body, monkeypatch)), _JiraInbound())


@pytest.fixture
def plain_cutover(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``reconciler.rich_text_cutover`` to its shipped default.

    Set through the canonical env override rather than a stub config object: the
    comment differ resolves its vendor adapter from the same config, so a
    stand-in carrying only this flag makes unrelated machinery raise.
    """
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", "off")


@pytest.mark.parametrize("cutover", ["off", "cloud"], ids=["plain", "rich"])
@pytest.mark.parametrize(
    "body", [_OVER_LENGTH_BODY, _ADF_ONLY_OVER_BODY], ids=["over-both", "over-adf-only"]
)
def test_fit_comment_is_the_text_the_send_path_lands(
    body: str, cutover: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 1 — the differ's key is the text the send path actually put on the wire.

    The two sides meet ONLY at the assertion: the right-hand side is decoded from
    the ``--body`` argument captured out of a real ``add_comment`` call, the
    left-hand side comes from the differ's own sanitizer. Neither is computed from
    the other, so a change to either one alone breaks this.

    Asserted under both ``rich_text_cutover`` settings: whatever the flag is, the
    two must agree, because they are supposed to BE the same transform. The flag is
    set through its canonical env override so the real resolution path runs.
    """
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", cutover)
    landed = oc._normalize_comment_body(json.loads(_send(body, monkeypatch)), _JiraInbound())

    assert _JiraSanitizer().fit_comment(body) == landed


def test_the_send_path_really_does_truncate_these_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guards the guard: the fixtures must actually engage the send path's fitter.

    Without this, a body short enough to pass through untouched would make the
    assertions above pass vacuously — both sides would just be the input.
    """
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", "off")
    for body in (_OVER_LENGTH_BODY, _ADF_ONLY_OVER_BODY):
        landed = oc._normalize_comment_body(json.loads(_send(body, monkeypatch)), _JiraInbound())
        assert len(landed) < len(body), "fixture precondition: the send path must truncate this"
        assert oc.RECONCILER_MARKER not in landed, "the Jira-side key is marker-free"


def _snapshot(jira_key: str, bodies: list[str]) -> dict[str, Any]:
    """Jira REST shape: fields["comment"]["comments"] (outer key "comment")."""
    comments = [{"id": str(100 + i), "body": b} for i, b in enumerate(bodies)]
    return {jira_key: {"comment": {"comments": comments, "total": len(comments)}}}


def test_an_already_landed_over_length_comment_is_not_re_posted(
    plain_cutover: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC 2 — the landed body equals the comparison key, so the differ emits nothing.

    The end-to-end statement of the bug: send an over-length comment, read back
    what Jira now holds, and ask the differ again. It must recognise its own
    landed body and stay silent. Driven with NO ``binding_store``, so the PRIMARY
    id-identity skip (repaired under aa7b) cannot mask the SECONDARY body-equality
    layer this ticket is about.
    """
    codec = AdfCodec(rich=False)
    landed = _landed_key(_OVER_LENGTH_BODY, monkeypatch)
    ticket = {"comments": [{"body": _OVER_LENGTH_BODY, "timestamp": "1-a"}]}

    out = oc._diff_comments(ticket, "REB-1", _snapshot("REB-1", [landed]), codec=codec)

    assert out == [], f"re-posted an already-landed comment; emitted {len(out)} mutation(s)"


def test_a_genuinely_new_over_length_comment_is_still_emitted(
    plain_cutover: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-regression: a key made too LOOSE would swallow real new comments."""
    codec = AdfCodec(rich=False)
    landed = _landed_key(_OVER_LENGTH_BODY, monkeypatch)
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


# ---------------------------------------------------------------------------
# Bug 17c3-2f6e-a5e0-438c: under the rich cutover the LOCAL key was decoded by
# a DIFFERENT decoder than the Jira side — `AdfCodec(rich=True).normalize_outbound`
# is a markdown round trip that escapes markdown-active characters and rewrites
# autolinks, while the Jira side decodes the fetched wire with
# `_normalize_comment_body` -> `adf_to_text` (no escaping). So an IN-LIMIT
# markdown comment never matched its own landed body and the secondary dedup
# layer was inert for every markdown-formatted comment, not just over-length
# ones. The landed side below is CAPTURED from the real ``add_comment`` (only
# ``_run_acli`` stubbed), exactly as the fixtures above — never re-derived.
# ---------------------------------------------------------------------------

# Well within every limit; every markdown-active construct the escaping touches
# (heading, emphasis, link target, code span, list) — the repro recorded on the
# ticket.
_IN_LIMIT_MARKDOWN_BODY = (
    "# Head\n\nSome *bold* text and a [link](http://x) plus `code`.\n\n- a\n- b"
)


def test_an_already_landed_in_limit_markdown_comment_is_not_re_posted_under_rich_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bug 17c3 AC 2 — with the cutover ON, an in-limit markdown comment that
    already landed must produce ZERO mutations on the next differ pass.

    Send the markdown body through the real send path, hand the differ a snapshot
    holding exactly the wire that landed, and ask it again. Driven with NO
    ``binding_store`` so the PRIMARY id-identity skip cannot mask the SECONDARY
    body-equality layer this ticket is about.
    """
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", "cloud")
    wire = json.loads(_send(_IN_LIMIT_MARKDOWN_BODY, monkeypatch))
    # Fixture precondition: the in-limit body must land UNTRUNCATED (this bug is
    # about decode asymmetry, not the fit — that is e339's cell above).
    landed_text = oc._normalize_comment_body(wire, _JiraInbound())
    assert "[truncated by reconciler]" not in landed_text

    ticket = {"comments": [{"body": _IN_LIMIT_MARKDOWN_BODY, "timestamp": "3-c"}]}
    out = oc._diff_comments(ticket, "REB-1", _snapshot("REB-1", [wire]), codec=AdfCodec(rich=True))

    assert out == [], (
        "re-posted an already-landed in-limit markdown comment under the rich "
        f"cutover; emitted {len(out)} mutation(s) — the local dedup key must be "
        "decoded by the SAME decoder as the Jira-side key"
    )


@pytest.mark.parametrize("cutover", ["off", "cloud"], ids=["plain", "rich"])
def test_a_genuinely_new_markdown_comment_is_still_emitted(
    cutover: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Counter-regression: a key made too LOOSE would swallow real new comments."""
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", cutover)
    wire = json.loads(_send(_IN_LIMIT_MARKDOWN_BODY, monkeypatch))
    ticket = {"comments": [{"body": "a *different* [comment](http://y)", "timestamp": "4-d"}]}

    out = oc._diff_comments(
        ticket, "REB-1", _snapshot("REB-1", [wire]), codec=AdfCodec(rich=cutover == "cloud")
    )

    assert len(out) == 1
    assert out[0]["action"] == "add"


def test_an_already_landed_over_length_markdown_comment_is_not_re_posted_under_rich_cutover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The truncation-notice facet: the old local key escaped the landed notice
    (``\\[truncated by reconciler\\]``), so even a fit-converged over-length body
    still diverged under the rich cutover. Same mechanism, boundary input.
    """
    monkeypatch.setenv("REBAR_RECONCILER_RICH_TEXT_CUTOVER", "cloud")
    body = "Line of markdown *prose* about the [reconciler](http://r). " * 800
    wire = json.loads(_send(body, monkeypatch))
    landed_text = oc._normalize_comment_body(wire, _JiraInbound())
    assert "[truncated by reconciler]" in landed_text, (
        "fixture precondition: the send path must truncate this body"
    )

    ticket = {"comments": [{"body": body, "timestamp": "5-e"}]}
    out = oc._diff_comments(ticket, "REB-1", _snapshot("REB-1", [wire]), codec=AdfCodec(rich=True))

    assert out == [], (
        f"re-posted an already-landed over-length markdown comment; emitted {len(out)}"
    )
