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


def _safe_submit_module():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).parents[2] / "scripts" / "gerrit_safe_submit.py"
    spec = importlib.util.spec_from_file_location("gerrit_safe_submit", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_safe_submit_accepts_abbreviated_current_revision(monkeypatch):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []

    def fake_credential(host: str) -> tuple[str, str]:
        return ("user", "pass")

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        if method == "GET":
            return {
                "current_revision": "deadbeef1234567890",
                "labels": {
                    "LLM-Review": {"approved": {"_account_id": 1}},
                    "Verified": {"approved": {"_account_id": 2}},
                },
                "unresolved_comment_count": 0,
                "submittable": True,
            }
        return {}

    monkeypatch.setattr(safe_submit, "_credential", fake_credential)
    monkeypatch.setattr(safe_submit, "_request", fake_request)
    monkeypatch.setenv("REBAR_SESSION_ID", "Session 1")

    assert safe_submit.main(["2764", "--revision", "deadbeef1234"]) == 0

    assert calls[0] == ("POST", "/a/changes/2764/hashtags", {"add": ["rebar-session-session-1"]})
    assert calls[-1] == ("POST", "/a/changes/2764/revisions/deadbeef1234567890/submit", {})


def test_safe_submit_refuses_stale_revision_before_submit(monkeypatch, capsys):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []

    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return {"current_revision": "cafebabe", "labels": {}}

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert safe_submit.main(["2764", "--revision", "deadbeef", "--hashtag", ""]) == 2

    assert all(not path.endswith("/submit") for _, path, _ in calls)
    assert "refusing stale submit" in capsys.readouterr().err


def test_safe_submit_refuses_without_approved_labels(monkeypatch):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return {
            "current_revision": "deadbeef1234567890",
            "labels": {
                "LLM-Review": {"all": [{"value": 1}]},
                "Verified": {"approved": {"_account_id": 2}},
            },
            "unresolved_comment_count": 0,
            "submittable": True,
        }

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert safe_submit.main(["2764", "--revision", "deadbeef1234", "--hashtag", ""]) == 3

    assert all(not path.endswith("/submit") for _, path, _ in calls)


def test_safe_submit_refuses_unresolved_comments(monkeypatch):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return {
            "current_revision": "deadbeef1234567890",
            "labels": {
                "LLM-Review": {"approved": {"_account_id": 1}},
                "Verified": {"approved": {"_account_id": 2}},
            },
            "unresolved_comment_count": 1,
            "submittable": True,
        }

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert safe_submit.main(["2764", "--revision", "deadbeef1234", "--hashtag", ""]) == 3

    assert all(not path.endswith("/submit") for _, path, _ in calls)


def test_safe_submit_refuses_too_short_revision_prefix(monkeypatch):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return {"current_revision": "deadbeef1234567890", "labels": {}}

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert safe_submit.main(["2764", "--revision", "deadbeef", "--hashtag", ""]) == 2

    assert all(not path.endswith("/submit") for _, path, _ in calls)
