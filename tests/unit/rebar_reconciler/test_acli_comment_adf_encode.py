"""acli comment send ADF-encodes with fit-BEFORE-serialize (emersed-specific-mutt).

The Cloud comment path (`acli_cli_ops.add_comment`) sent ``--body <plain str>``
with no ADF encode, unlike the description path. This story routes the body
through the same ``RichTextCodec`` the description path uses:
``AdfCodec(rich="cloud" in cutover_clients())`` — FIT the text first (measured
against the serialized-ADF limit) THEN ``json.dumps(to_wire(fitted))``. The
plaintext ``_sanitize_comment`` guard must never run on serialized ADF (it would
truncate the JSON mid-structure into a payload ACLI rejects). ``--body`` accepts
ADF (confirmed by ``acli jira workitem comment create --help``).

Hermetic: the acli subprocess is stubbed — no acli, no Jira.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def acli_ops():
    from rebar_reconciler.adapters.jira import acli_cli_ops

    return acli_cli_ops


def _capture_run(monkeypatch, acli_ops):
    captured: dict = {}

    class _Result:
        stdout = '{"id": "10001"}'

    def _fake_run(cmd, acli_cmd=None):
        captured["cmd"] = cmd
        return _Result()

    monkeypatch.setattr(acli_ops.acli_subprocess, "_run_acli", _fake_run)
    return captured


def _body_arg(cmd: list[str]) -> str:
    return cmd[cmd.index("--body") + 1]


def test_comment_rendered_through_codec(monkeypatch, acli_ops):
    """AC: outbound Cloud comment bodies render through ``RichTextCodec.to_wire``.
    The --body value is a serialized ADF document, not a bare plain string."""
    captured = _capture_run(monkeypatch, acli_ops)
    out = acli_ops.add_comment("DIG-1", "hello world")
    assert out == {"id": "10001"}

    body_arg = _body_arg(captured["cmd"])
    parsed = json.loads(body_arg)  # plain text would not be valid JSON ADF
    assert isinstance(parsed, dict)
    assert parsed.get("type") == "doc"


def test_comment_adf_encode_fits_before_serialize(monkeypatch, acli_ops):
    """AC: fit-then-encode ordering — an over-length body is fit as TEXT first, so
    the serialized ADF stays under Jira's limit AND remains valid JSON (the
    plaintext ``_sanitize_comment`` guard never slices serialized JSON mid-doc)."""
    captured = _capture_run(monkeypatch, acli_ops)
    huge = "x" * 60000
    acli_ops.add_comment("DIG-1", huge)

    body_arg = _body_arg(captured["cmd"])
    parsed = json.loads(body_arg)  # must not be truncated mid-JSON
    assert parsed.get("type") == "doc"
    assert len(body_arg) <= 32767, "serialized ADF must be within Jira's comment limit"
