from __future__ import annotations

import subprocess

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


def test_safe_submit_accepts_full_current_revision(monkeypatch):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []

    def fake_credential(host: str) -> tuple[str, str]:
        return ("user", "pass")

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        if method == "GET":
            return {
                "current_revision": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
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

    assert safe_submit.main(["2764", "--revision", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"]) == 0

    assert calls[1] == ("POST", "/a/changes/2764/hashtags", {"add": ["rebar-session-session-1"]})
    assert calls[-1] == (
        "POST",
        "/a/changes/2764/revisions/deadbeefdeadbeefdeadbeefdeadbeefdeadbeef/submit",
        {},
    )


def test_safe_submit_refuses_stale_revision_before_submit(monkeypatch, capsys):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []

    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return {"current_revision": "cafebabe" * 5, "labels": {}}

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert safe_submit.main(["2764", "--revision", "deadbeef" * 5, "--hashtag", ""]) == 2

    assert all(not path.endswith("/submit") for _, path, _ in calls)
    assert "refusing stale submit" in capsys.readouterr().err


def test_safe_submit_refuses_without_approved_labels(monkeypatch):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return {
            "current_revision": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "labels": {
                "LLM-Review": {"all": [{"value": 1}]},
                "Verified": {"approved": {"_account_id": 2}},
            },
            "unresolved_comment_count": 0,
            "submittable": True,
        }

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert (
        safe_submit.main(
            ["2764", "--revision", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "--hashtag", ""]
        )
        == 3
    )

    assert all(not path.endswith("/submit") for _, path, _ in calls)


def test_safe_submit_refuses_unresolved_comments(monkeypatch):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return {
            "current_revision": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "labels": {
                "LLM-Review": {"approved": {"_account_id": 1}},
                "Verified": {"approved": {"_account_id": 2}},
            },
            "unresolved_comment_count": 1,
            "submittable": True,
        }

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert (
        safe_submit.main(
            ["2764", "--revision", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "--hashtag", ""]
        )
        == 3
    )

    assert all(not path.endswith("/submit") for _, path, _ in calls)


def _assert_form_refusal(err: str) -> None:
    assert "not a full 40-character commit SHA" in err
    assert "git rev-parse HEAD" in err
    assert "stale" not in err


def test_safe_submit_refuses_abbreviated_revision_prefix(monkeypatch, capsys):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return {"current_revision": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "labels": {}}

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert safe_submit.main(["2764", "--revision", "deadbeef", "--hashtag", ""]) == 2

    assert all(not path.endswith("/submit") for _, path, _ in calls)
    _assert_form_refusal(capsys.readouterr().err)


@pytest.mark.parametrize(
    "revision",
    [
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeeg",  # 40 chars, one non-hex
        "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef0",  # 41 hex chars
        "",
        "HEAD",
    ],
)
def test_safe_submit_refuses_malformed_revision_without_gerrit_request(
    monkeypatch, capsys, revision
):
    safe_submit = _safe_submit_module()

    calls: list[tuple[str, str, dict | None]] = []
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        return dict(_GREEN_DETAIL)

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert safe_submit.main(["2764", "--revision", revision, "--hashtag", ""]) == 2

    assert calls == []
    _assert_form_refusal(capsys.readouterr().err)


def test_safe_submit_names_missing_git_credential(monkeypatch, capsys):
    safe_submit = _safe_submit_module()

    def missing_credential(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            128,
            ["git", "credential", "fill"],
            stderr="fatal: unable to get password from user\n",
        )

    monkeypatch.setattr(safe_submit.subprocess, "run", missing_credential)

    assert (
        safe_submit.main(
            ["2764", "--revision", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "--hashtag", ""]
        )
        == 4
    )

    err = capsys.readouterr().err
    assert "missing Gerrit credential for rebar.solutions.navateam.com" in err
    assert "unable to get password from user" in err
    assert "CalledProcessError" not in err


def test_safe_submit_returns_stale_when_submit_races_to_409(monkeypatch, capsys):
    """Another session uploads a patch set between the vote check and the submit POST."""
    import urllib.error

    safe_submit = _safe_submit_module()

    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))
    gets = []

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        if method == "POST" and path.endswith("/submit"):
            raise urllib.error.HTTPError(path, 409, "conflict", {}, None)
        if method == "GET":
            gets.append(path)
            return {
                "current_revision": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
                if len(gets) == 1
                else "cafebabecafebabecafebabecafebabecafebabe",
                "labels": {
                    "LLM-Review": {"approved": {"_account_id": 1}},
                    "Verified": {"approved": {"_account_id": 2}},
                },
                "unresolved_comment_count": 0,
                "submittable": True,
            }
        return {}

    monkeypatch.setattr(safe_submit, "_request", fake_request)

    assert (
        safe_submit.main(
            ["2764", "--revision", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "--hashtag", ""]
        )
        == 2
    )

    err = capsys.readouterr().err
    assert "no longer current" in err
    assert "cafebabecafebabecafebabecafebabecafebabe" in err


@pytest.mark.parametrize("inherited", [None, "1"])
def test_credential_lookup_never_prompts_on_a_terminal(monkeypatch, inherited):
    """With no helper and a controlling terminal, git credential fill asks for a username on
    the tty and an agent session blocks on it; the lookup must fail fast instead."""
    safe_submit = _safe_submit_module()
    seen: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(cmd, 0, stdout="username=u\npassword=p\n", stderr="")

    if inherited is None:
        monkeypatch.delenv("GIT_TERMINAL_PROMPT", raising=False)
    else:
        monkeypatch.setenv("GIT_TERMINAL_PROMPT", inherited)
    monkeypatch.setattr(safe_submit.subprocess, "run", fake_run)

    assert safe_submit._credential("rebar.solutions.navateam.com") == ("u", "p")
    assert seen["GIT_TERMINAL_PROMPT"] == "0"


_GREEN_DETAIL = {
    "current_revision": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    "labels": {
        "LLM-Review": {"approved": {"_account_id": 1}},
        "Verified": {"approved": {"_account_id": 2}},
    },
    "unresolved_comment_count": 0,
    "submittable": True,
}


def _submit_failing_with(monkeypatch, status: int, body: bytes, reread=None):
    """Load the script with a green change whose submit POST fails with ``status``/``body``.

    ``reread`` is what any GET after the first returns: a revision SHA, or an exception to
    raise. By default the change still reports the submitted revision as current.
    """
    import io
    import urllib.error

    safe_submit = _safe_submit_module()
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))
    gets = []

    def fake_request(base_url: str, method: str, path: str, auth: str, body_=None) -> dict:
        if method == "POST" and path.endswith("/submit"):
            raise urllib.error.HTTPError(path, status, "refused", {}, io.BytesIO(body))
        if method != "GET":
            return {}
        gets.append(path)
        if len(gets) > 1 and isinstance(reread, Exception):
            raise reread
        if len(gets) > 1 and reread:
            return {**_GREEN_DETAIL, "current_revision": reread}
        return dict(_GREEN_DETAIL)

    monkeypatch.setattr(safe_submit, "_request", fake_request)
    return safe_submit


_FULL_SHA_ARGS = ["2764", "--revision", "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "--hashtag", ""]


def test_safe_submit_409_prints_gerrit_message_and_status(monkeypatch, capsys):
    safe_submit = _submit_failing_with(
        monkeypatch, 409, b'{"message": "revision abc is not current revision"}'
    )

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    err = capsys.readouterr().err
    assert "revision abc is not current revision" in err
    assert "409" in err


def test_safe_submit_409_strips_xssi_prefix_from_body(monkeypatch, capsys):
    safe_submit = _submit_failing_with(monkeypatch, 409, b')]}\'\n{"message": "submit blocked"}')

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    err = capsys.readouterr().err
    assert "submit blocked" in err
    assert ")]}'" not in err


def test_safe_submit_409_prints_plain_text_body_as_is(monkeypatch, capsys):
    body = (
        b"Cannot rebase db3f3e396f1: Change 3169 could not be rebased due to a conflict "
        b"during merge.\n\nmerge conflict(s):\n* docs/release-notes.md\n"
    )
    safe_submit = _submit_failing_with(monkeypatch, 409, body)

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    err = capsys.readouterr().err
    assert "could not be rebased due to a conflict during merge" in err
    assert "* docs/release-notes.md" in err


def test_safe_submit_409_prints_json_without_message_as_is(monkeypatch, capsys):
    safe_submit = _submit_failing_with(monkeypatch, 409, b'{"status": "CONFLICT"}')

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    assert '{"status": "CONFLICT"}' in capsys.readouterr().err


def test_safe_submit_409_with_empty_body_does_not_claim_staleness(monkeypatch, capsys):
    safe_submit = _submit_failing_with(monkeypatch, 409, b"")

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    err = capsys.readouterr().err
    assert "rejected the submit (HTTP 409)" in err
    assert "no longer current" not in err


_CONFLICT_BODY = (
    b"Cannot rebase db3f3e396f1: Change 3169 could not be rebased due to a conflict "
    b"during merge.\n\nmerge conflict(s):\n* docs/release-notes.md\n"
)


def test_safe_submit_409_merge_conflict_names_conflict_not_staleness(monkeypatch, capsys):
    safe_submit = _submit_failing_with(monkeypatch, 409, _CONFLICT_BODY)

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    err = capsys.readouterr().err
    assert "docs/release-notes.md" in err
    assert "merge conflict" in err
    assert "rebase it onto origin/main" in err
    assert "no longer current" not in err


def test_safe_submit_409_after_newer_patch_set_reports_staleness(monkeypatch, capsys):
    newer = "cafebabecafebabecafebabecafebabecafebabe"
    safe_submit = _submit_failing_with(
        monkeypatch,
        409,
        b"revision deadbeefdeadbeefdeadbeefdeadbeefdeadbeef is not current revision",
        reread=newer,
    )

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    err = capsys.readouterr().err
    assert "no longer current" in err
    assert newer in err


def test_safe_submit_other_409_reports_gerrit_reason_without_guessing(monkeypatch, capsys):
    safe_submit = _submit_failing_with(
        monkeypatch, 409, b"submit requirement 'Code-Review' is unsatisfied"
    )

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    err = capsys.readouterr().err
    assert "rejected the submit (HTTP 409)" in err
    assert "submit requirement 'Code-Review' is unsatisfied" in err
    assert "no longer current" not in err
    assert "merge conflict" not in err


def test_safe_submit_409_when_reread_fails_still_names_conflict(monkeypatch, capsys):
    import urllib.error

    safe_submit = _submit_failing_with(
        monkeypatch, 409, _CONFLICT_BODY, reread=urllib.error.URLError("down")
    )

    assert safe_submit.main(_FULL_SHA_ARGS) == 2

    err = capsys.readouterr().err
    assert "merge conflict" in err
    assert "no longer current" not in err


# ── Errors after the vote check reconcile against Gerrit (chewy-flavoury-milksnake) ──

_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"


def _post_vote_failure(monkeypatch, *, submit_exc=None, hashtag_exc=None, reread=None):
    """A green change whose hashtag or submit POST raises; later GETs return ``reread``
    (a dict merged over the green detail, or an exception to raise)."""
    safe_submit = _safe_submit_module()
    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))
    calls: list[tuple[str, str]] = []

    def fake_request(base_url: str, method: str, path: str, auth: str, body_=None) -> dict:
        calls.append((method, path))
        if method == "POST" and path.endswith("/hashtags") and hashtag_exc is not None:
            raise hashtag_exc
        if method == "POST" and path.endswith("/submit"):
            if submit_exc is not None:
                raise submit_exc
            return {}
        if method != "GET":
            return {}
        if len([c for c in calls if c[0] == "GET"]) == 1:
            return dict(_GREEN_DETAIL)
        if isinstance(reread, Exception):
            raise reread
        return {**_GREEN_DETAIL, **(reread or {})}

    monkeypatch.setattr(safe_submit, "_request", fake_request)
    return safe_submit, calls


_MERGED = {"status": "MERGED", "current_revision": "f" * 40, "revisions": {_SHA: {}, "f" * 40: {}}}
_NOT_MERGED = {"status": "NEW", "revisions": {_SHA: {}}}


def _http(status: int, body: bytes):
    import io
    import urllib.error

    return urllib.error.HTTPError("submit", status, "err", {}, io.BytesIO(body))


def test_submit_http_error_after_merge_exits_0(monkeypatch, capsys):
    safe_submit, _ = _post_vote_failure(
        monkeypatch, submit_exc=_http(502, b"bad gateway"), reread=_MERGED
    )
    assert safe_submit.main(_FULL_SHA_ARGS) == 0
    assert "merged" in capsys.readouterr().out


def test_submit_timeout_after_merge_exits_0(monkeypatch, capsys):
    import urllib.error

    safe_submit, _ = _post_vote_failure(
        monkeypatch, submit_exc=urllib.error.URLError(TimeoutError("timed out")), reread=_MERGED
    )
    assert safe_submit.main(_FULL_SHA_ARGS) == 0
    assert "merged" in capsys.readouterr().out


def test_submit_unparseable_body_after_merge_exits_0(monkeypatch, capsys):
    safe_submit, _ = _post_vote_failure(
        monkeypatch, submit_exc=ValueError("Expecting value"), reread=_MERGED
    )
    assert safe_submit.main(_FULL_SHA_ARGS) == 0
    assert "merged" in capsys.readouterr().out


def test_submit_http_error_not_merged_exits_1_with_body(monkeypatch, capsys):
    safe_submit, _ = _post_vote_failure(
        monkeypatch, submit_exc=_http(500, b"internal boom detail"), reread=_NOT_MERGED
    )
    assert safe_submit.main(_FULL_SHA_ARGS) == 1
    err = capsys.readouterr().err
    assert "internal boom detail" in err
    assert "500" in err


def test_merged_check_requires_the_submitted_revision(monkeypatch):
    other = {"status": "MERGED", "revisions": {"c" * 40: {}}}
    safe_submit, _ = _post_vote_failure(monkeypatch, submit_exc=_http(502, b"x"), reread=other)
    assert safe_submit.main(_FULL_SHA_ARGS) == 1


def test_submit_error_and_failed_reread_exits_1_naming_both(monkeypatch, capsys):
    import urllib.error

    safe_submit, _ = _post_vote_failure(
        monkeypatch,
        submit_exc=urllib.error.URLError("connection reset"),
        reread=urllib.error.URLError("still down"),
    )
    assert safe_submit.main(_FULL_SHA_ARGS) == 1
    err = capsys.readouterr().err
    assert "connection reset" in err
    assert "still down" in err


def test_hashtag_failure_warns_and_still_submits(monkeypatch, capsys):
    safe_submit, calls = _post_vote_failure(monkeypatch, hashtag_exc=_http(500, b"tag boom"))
    assert safe_submit.main(["2764", "--revision", _SHA, "--hashtag", "rebar-session-x"]) == 0
    assert calls[-1][1].endswith(f"/revisions/{_SHA}/submit")
    assert "hashtag" in capsys.readouterr().err.lower()


# ── an already-closed change (bug froggy-brightish-xiphias) ──────────────────────
# Gerrit omits the labels' ``approved`` summary on a closed change, so re-running the script
# after a merge used to report "missing live labels" (exit 3) for votes that exist.

_MERGED_SHA = "a" * 40
_EARLIER_SHA = "b" * 40
_OTHER_SHA = "c" * 40
_CLOSED_LABELS = {
    "LLM-Review": {"all": [{"value": 1}]},
    "Verified": {"all": [{"value": 1}]},
}


def _closed_change(monkeypatch, status: str, revisions: tuple[str, ...]) -> list[tuple]:
    """Patch the module's Gerrit I/O for a change in ``status``; return the recorded calls."""
    safe_submit = _safe_submit_module()
    calls: list[tuple] = []

    def fake_request(base_url: str, method: str, path: str, auth: str, body=None) -> dict:
        calls.append((method, path, body))
        assert method == "GET", f"a closed change must not be POSTed to: {path}"
        return {
            "status": status,
            "current_revision": _MERGED_SHA,
            "revisions": {sha: {} for sha in revisions},
            "labels": _CLOSED_LABELS,
            "unresolved_comment_count": 0,
            "submittable": False,
        }

    monkeypatch.setattr(safe_submit, "_credential", lambda host: ("user", "pass"))
    monkeypatch.setattr(safe_submit, "_request", fake_request)
    monkeypatch.setenv("REBAR_SESSION_ID", "Session 1")
    return safe_submit, calls


def test_safe_submit_reports_a_merged_change_at_the_inspected_revision(monkeypatch, capsys):
    safe_submit, calls = _closed_change(monkeypatch, "MERGED", (_MERGED_SHA,))

    assert safe_submit.main(["3393", "--revision", _MERGED_SHA]) == 0

    out = capsys.readouterr()
    assert "already merged" in out.out
    assert _MERGED_SHA in out.out
    assert "missing live labels" not in out.err
    assert all(method == "GET" for method, _, _ in calls)


def test_safe_submit_counts_a_rebased_merge_of_the_inspected_revision(monkeypatch, capsys):
    """Rebase-If-Necessary merges a newer patch set; the inspected one is still among them."""
    safe_submit, calls = _closed_change(monkeypatch, "MERGED", (_EARLIER_SHA, _MERGED_SHA))

    assert safe_submit.main(["3393", "--revision", _EARLIER_SHA]) == 0

    assert "already merged" in capsys.readouterr().out
    assert all(method == "GET" for method, _, _ in calls)


def test_safe_submit_refuses_a_merged_change_at_another_revision(monkeypatch, capsys):
    safe_submit, calls = _closed_change(monkeypatch, "MERGED", (_MERGED_SHA,))

    assert safe_submit.main(["3393", "--revision", _OTHER_SHA]) == 2

    err = capsys.readouterr().err
    assert "MERGED" in err
    assert _MERGED_SHA in err and _OTHER_SHA in err
    assert all(method == "GET" for method, _, _ in calls)


def test_safe_submit_refuses_an_abandoned_change(monkeypatch, capsys):
    safe_submit, calls = _closed_change(monkeypatch, "ABANDONED", (_MERGED_SHA,))

    assert safe_submit.main(["3393", "--revision", _MERGED_SHA]) == 3

    err = capsys.readouterr().err
    assert "abandoned" in err.lower()
    assert "missing live labels" not in err
    assert all(method == "GET" for method, _, _ in calls)
