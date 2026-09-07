"""Keep review-bot tokens out of uvicorn access logs.

Query parameters appear in logged request lines, so webhook and rerun secrets
use the ``X-Rebar-Token`` header. A logging filter also redacts any remaining
``token=...`` query value before emission. The oracle checks emitted log text
rather than handler behavior.
"""

from __future__ import annotations

import logging

import pytest

from rebar.review_bot.config import (
    TokenRedactingFilter,
    install_access_log_redaction,
)

SENTINEL = "s3nt1nel-bot-token-DO-NOT-LOG"


def _uvicorn_access_record(full_path: str) -> logging.LogRecord:
    """A LogRecord shaped exactly like uvicorn's access log emits: the request line is
    passed as ``%``-args ``(client, method, full_path, http_version, status)`` against the
    ``'%s - "%s %s HTTP/%s" %d'`` template — the query string lives in ``full_path``."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:34184", "POST", full_path, "1.1", 202),
        exc_info=None,
    )


def test_redaction_filter_scrubs_query_token_but_keeps_route_and_status():
    """RED without the filter: the sentinel survives into the formatted access line.

    GREEN: the value after ``token=`` is redacted while route + status remain, so the
    observability greps still work but the secret is gone."""
    record = _uvicorn_access_record(f"/rerun?token={SENTINEL}&change=1323")
    TokenRedactingFilter().filter(record)
    message = record.getMessage()

    assert SENTINEL not in message, f"token must be scrubbed from the access line: {message!r}"
    assert "token=" in message and "<redacted>" in message, message
    # route + status (the load-bearing observability data) survive
    assert "/rerun" in message
    assert "change=1323" in message
    assert "202" in message


def test_redaction_filter_scrubs_webhook_query_token():
    """The /webhook request line (token as the sole query param) is scrubbed too."""
    record = _uvicorn_access_record(f"/webhook?token={SENTINEL}")
    TokenRedactingFilter().filter(record)
    message = record.getMessage()
    assert SENTINEL not in message, message
    assert "/webhook" in message and "<redacted>" in message


def test_install_access_log_redaction_is_idempotent_and_wired():
    """The filter is installed on the ``uvicorn.access`` logger, and re-installing does
    not stack duplicate filters (import-time double call under reload)."""
    install_access_log_redaction()
    install_access_log_redaction()
    access = logging.getLogger("uvicorn.access")
    redactors = [f for f in access.filters if isinstance(f, TokenRedactingFilter)]
    assert len(redactors) == 1, f"exactly one redaction filter must be installed: {access.filters}"

    # And an access record routed through that logger's filters is scrubbed.
    record = _uvicorn_access_record(f"/webhook?token={SENTINEL}")
    assert all(f.filter(record) for f in access.filters)
    assert SENTINEL not in record.getMessage()


# ── option (a): the token moves to a header, out of the request line entirely ──────────


def _app_and_client(monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from rebar.review_bot import app as app_module
    from rebar.review_bot.config import ReceiverConfig

    cfg = ReceiverConfig(gerrit_bot_token=SENTINEL, webhook_token=SENTINEL)
    app_module.app.state.config = cfg
    return app_module, TestClient(app_module.app)


def test_rerun_authenticates_via_header_and_token_never_logged(monkeypatch, caplog):
    """/rerun accepts the secret as ``X-Rebar-Token`` and the sentinel appears in NO log
    record the app emits — the request line (which uvicorn logs) carries no token at all.

    RED before option (a): the operator recipe put the token in the URL, so a driven
    request logged it."""
    _app_module, client = _app_and_client(monkeypatch)

    def fake_get_change_event(self, change):
        return {
            "change": {"id": "proj~main~I123", "project": "rebar"},
            "patchSet": {"revision": "deadbeef"},
        }

    from rebar.review_bot import gerrit_client

    monkeypatch.setattr(gerrit_client.GerritClient, "get_change_event", fake_get_change_event)
    monkeypatch.setattr(
        gerrit_client.GerritClient,
        "reset_llm_review_vote",
        lambda self, change_id, revision: 200,
    )
    monkeypatch.setattr(
        gerrit_client.GerritClient,
        "has_llm_review_vote",
        lambda self, change_id, revision: False,
    )

    class NoopStore:
        def __init__(self, _path):
            pass

        def reset_attempts(self, *args):
            pass

    monkeypatch.setattr("rebar.review_bot.dedup.DedupStore", NoopStore)

    with caplog.at_level(logging.DEBUG):
        resp = client.post(
            "/rerun",
            headers={"X-Rebar-Token": SENTINEL},
            json={"change": "1323"},
        )
    assert resp.status_code == 202, resp.text
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert SENTINEL not in joined, f"token must never be logged; found in:\n{joined}"


def test_rerun_rejects_missing_and_wrong_token(monkeypatch):
    """Auth is unchanged in strength: no token / wrong token → 401 on both transports."""
    _app_module, client = _app_and_client(monkeypatch)
    assert client.post("/rerun", json={"change": "1"}).status_code == 401
    assert (
        client.post("/rerun", headers={"X-Rebar-Token": "wrong"}, json={"change": "1"}).status_code
        == 401
    )


def test_webhook_authenticates_via_header(monkeypatch):
    """/webhook also accepts the header form (Gerrit's webhooks plugin can send a header),
    so the inbound path can be moved off the query string too."""
    _app_module, client = _app_and_client(monkeypatch)
    resp = client.post(
        "/webhook",
        headers={"X-Rebar-Token": SENTINEL},
        json={"type": "patchset-created"},
    )
    assert resp.status_code == 202, resp.text
    assert client.post("/webhook", json={"type": "patchset-created"}).status_code == 401


def test_webhook_still_accepts_query_token_for_backward_compat(monkeypatch):
    """Backward-compat: until Gerrit's webhooks.config is re-pushed to send the header, the
    live query-string path must keep authenticating — the redaction filter (option c) is
    what protects that value in the log, not rejection."""
    _app_module, client = _app_and_client(monkeypatch)
    resp = client.post(
        f"/webhook?token={SENTINEL}",
        json={"type": "patchset-created"},
    )
    assert resp.status_code == 202, resp.text
