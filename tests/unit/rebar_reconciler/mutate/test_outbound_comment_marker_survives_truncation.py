"""The RECONCILER_MARKER must survive outbound comment truncation (bug 5931).

``_decorate_outbound_comment`` appends ``RECONCILER_MARKER`` as the LAST characters
of the outbound body, and every comment fitter truncates from the RIGHT. So for any
body over Jira's limit the send path cut the marker off, and the comment landed
UNMARKED — invisible to the inbound loop-breaker
(``inbound_collection_diffs.py``'s ``RECONCILER_MARKER in body_text`` test) and to
any marker-based cleanup. 836 such comments accumulated live across REB-1567,
REB-1931 and REB-2605.

The oracle is the observable send payload: the ``--body`` argument the Cloud comment
path hands ACLI. Hermetic — the acli subprocess is stubbed; no acli, no Jira.
"""

from __future__ import annotations

import json

import pytest

_TRUNCATION_SUFFIX = " … [truncated by reconciler]"
_JIRA_COMMENT_MAX_CHARS = 32767


@pytest.fixture()
def acli_ops():
    from rebar_reconciler.adapters.jira import acli_cli_ops

    return acli_cli_ops


@pytest.fixture()
def outbound():
    from rebar_reconciler import outbound_comments

    return outbound_comments


def _capture_run(monkeypatch, acli_ops) -> dict:
    captured: dict = {}

    class _Result:
        stdout = '{"id": "10001"}'

    def _fake_run(cmd, acli_cmd=None):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(acli_ops.acli_subprocess, "_run_acli", _fake_run)
    return captured


def _landed_text(captured: dict) -> tuple[str, str]:
    """``(serialized --body, the text a Jira reader/the inbound differ sees)``."""
    from rebar_reconciler.adapters.jira import adf

    cmd = captured["cmd"]
    body_arg = cmd[cmd.index("--body") + 1]
    return body_arg, adf.adf_to_text(json.loads(body_arg))


@pytest.mark.parametrize(
    ("label", "raw_len"),
    [
        # Grossly over-length: truncation is unambiguous.
        ("far_over_limit", 60_000),
        # Over the limit ONLY because the marker decoration pushes it over — the thin
        # case a single-input fix would miss.
        ("over_limit_by_the_marker", _JIRA_COMMENT_MAX_CHARS - 10),
    ],
)
def test_over_length_comment_lands_with_its_marker(
    monkeypatch, acli_ops, outbound, label: str, raw_len: int
) -> None:
    """An over-length outbound comment must land WITH the loop-breaker marker.

    Without it the inbound differ cannot recognise the comment as our own echo, so
    the bridge re-mirrors it and duplicates accumulate.
    """
    captured = _capture_run(monkeypatch, acli_ops)
    decorated = outbound._decorate_outbound_comment("x" * raw_len)
    assert decorated.endswith(outbound.RECONCILER_MARKER), "precondition: body is decorated"

    acli_ops.add_comment("DIG-1", decorated)
    body_arg, landed = _landed_text(captured)

    # The existing size contract is not broken.
    assert len(body_arg) <= _JIRA_COMMENT_MAX_CHARS, (
        f"{label}: serialized ADF must stay within Jira's comment limit"
    )
    # Content really was cut, so this case genuinely exercises truncation.
    assert len(landed) < len(decorated), f"{label}: precondition — the body was truncated"
    # THE BUG: the marker is cut off with the tail.
    assert outbound.RECONCILER_MARKER in landed, (
        f"{label}: the truncated comment landed WITHOUT RECONCILER_MARKER — the inbound "
        f"loop-breaker cannot recognise it as our own echo"
    )


def test_truncated_comment_still_shows_the_truncation_suffix(
    monkeypatch, acli_ops, outbound
) -> None:
    """Preserving the marker must not drop the visible truncation notice."""
    captured = _capture_run(monkeypatch, acli_ops)
    acli_ops.add_comment("DIG-1", outbound._decorate_outbound_comment("x" * 60_000))
    _, landed = _landed_text(captured)

    assert _TRUNCATION_SUFFIX in landed, (
        "a truncated body must still tell a Jira reader it was shortened"
    )
    assert outbound.RECONCILER_MARKER in landed


def test_the_inbound_loop_breaker_recognises_a_truncated_echo(
    monkeypatch, acli_ops, outbound
) -> None:
    """End-to-end: the landed body satisfies the inbound differ's echo predicate."""
    from rebar_reconciler import inbound_collection_diffs as icd

    captured = _capture_run(monkeypatch, acli_ops)
    acli_ops.add_comment("DIG-1", outbound._decorate_outbound_comment("x" * 60_000))
    _, landed = _landed_text(captured)

    assert icd.RECONCILER_MARKER in landed, (
        "the inbound loop-breaker tests exactly this membership; an over-length echo "
        "must not be able to slip past it"
    )


def test_under_limit_comment_is_unchanged(monkeypatch, acli_ops, outbound) -> None:
    """Regression guard: the common (in-limit) path must be byte-identical."""
    captured = _capture_run(monkeypatch, acli_ops)
    decorated = outbound._decorate_outbound_comment("a short comment")
    acli_ops.add_comment("DIG-1", decorated)
    _, landed = _landed_text(captured)

    assert landed == decorated
    assert _TRUNCATION_SUFFIX not in landed
