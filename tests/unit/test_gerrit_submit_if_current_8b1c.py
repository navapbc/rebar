from __future__ import annotations

import pytest

from rebar.review_bot.config import ReceiverConfig
from rebar.review_bot.gerrit_client import GerritClient, GerritError


def _cfg(tmp_path) -> ReceiverConfig:
    return ReceiverConfig(
        llm_review_max_value=1,
        llm_review_block_value=-1,
        dedup_db_path=str(tmp_path / "voted.db"),
        gerrit_bot_token="tok",
        webhook_token="tok",
        project="rebar",
    )


def test_submit_revision_uses_revision_scoped_endpoint(tmp_path):
    client = GerritClient(_cfg(tmp_path))
    captured: dict = {}

    def fake_request(method, path, *, body=None):
        captured.update(method=method, path=path, body=body)
        return 200, ")]}'\n{}"

    client._request = fake_request  # type: ignore[method-assign]

    assert client.submit_revision("rebar~main~Iabc", "deadbeef") == 200

    assert captured["method"] == "POST"
    assert captured["path"].endswith("/changes/rebar~main~Iabc/revisions/deadbeef/submit")
    assert captured["body"] == {}


def test_submit_revision_preserves_gerrit_stale_revision_409(tmp_path):
    client = GerritClient(_cfg(tmp_path))
    client._request = lambda *args, **kwargs: (_ for _ in ()).throw(  # type: ignore[method-assign]
        GerritError("revision deadbeef is not current revision", status=409)
    )

    with pytest.raises(GerritError) as raised:
        client.submit_revision("rebar~main~Iabc", "deadbeef")

    assert raised.value.status == 409
    assert "not current" in str(raised.value)


def test_add_session_hashtag_uses_gerrit_native_hashtags(tmp_path):
    client = GerritClient(_cfg(tmp_path))
    captured: dict = {}

    def fake_request(method, path, *, body=None):
        captured.update(method=method, path=path, body=body)
        return 200, ")]}'\n{}"

    client._request = fake_request  # type: ignore[method-assign]

    assert client.add_session_hashtag("rebar~main~Iabc", "rebar-session-d4d8a7bd") == 200

    assert captured["method"] == "POST"
    assert captured["path"].endswith("/changes/rebar~main~Iabc/hashtags")
    assert captured["body"] == {"add": ["rebar-session-d4d8a7bd"]}
