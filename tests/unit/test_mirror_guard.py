"""Behavioral + contractual tests for the mirror-lock guardian (ticket a774, epic b744).

Behavioral: does each verdict function return the right health for in-sync/diverged
replication and for a locked ruleset vs each drift mutation? Does the CLI runner map
health → the right exit code?

Contractual: the verdict output SCHEMA (keys) and the CLI EXIT CODES are pinned so a
scheduler (GitHub Actions / a CloudWatch probe) can rely on them. All I/O seams are
monkeypatched — the unit suite's socket guard forbids real network.
"""

from __future__ import annotations

import urllib.error

import pytest

from rebar import mirror_guard

pytestmark = pytest.mark.unit


def _locked_ruleset() -> dict:
    """A ruleset object that satisfies the full mirror-lock contract."""
    return {
        "id": 18402431,
        "name": "gerrit-mirror-lock-main",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "rules": [{"type": "update"}, {"type": "deletion"}, {"type": "non_fast_forward"}],
        "bypass_actors": [{"actor_type": "DeployKey", "bypass_mode": "always", "actor_id": None}],
    }


# --- Behavioral: replication_verdict ---------------------------------------
def test_replication_in_sync_is_healthy() -> None:
    v = mirror_guard.replication_verdict("abc123", "abc123")
    assert v["healthy"] is True


def test_replication_diverged_is_unhealthy() -> None:
    v = mirror_guard.replication_verdict("gerrit_sha", "old_github_sha")
    assert v["healthy"] is False
    assert v["gerrit_sha"] == "gerrit_sha" and v["github_sha"] == "old_github_sha"


@pytest.mark.parametrize("g,h", [(None, "x"), ("x", None), (None, None), ("", "")])
def test_replication_missing_sha_is_unhealthy(g, h) -> None:
    assert mirror_guard.replication_verdict(g, h)["healthy"] is False


# --- Behavioral: ruleset_verdict (locked vs each drift) --------------------
def test_ruleset_locked_is_healthy() -> None:
    v = mirror_guard.ruleset_verdict(_locked_ruleset())
    assert v["healthy"] is True
    assert v["reasons"] == []


def test_ruleset_deleted_is_unhealthy() -> None:
    v = mirror_guard.ruleset_verdict(None)
    assert v["healthy"] is False
    assert "UNPROTECTED" in v["reason"]


def test_ruleset_disabled_enforcement_is_drift() -> None:
    rs = _locked_ruleset()
    rs["enforcement"] = "disabled"
    assert mirror_guard.ruleset_verdict(rs)["healthy"] is False


def test_ruleset_missing_rule_is_drift() -> None:
    rs = _locked_ruleset()
    rs["rules"] = [r for r in rs["rules"] if r["type"] != "non_fast_forward"]
    v = mirror_guard.ruleset_verdict(rs)
    assert v["healthy"] is False
    assert any("non_fast_forward" in r for r in v["reasons"])


def test_ruleset_extra_bypass_actor_is_drift() -> None:
    """An admin/team bypass added out-of-band re-opens a human merge path."""
    rs = _locked_ruleset()
    rs["bypass_actors"].append(
        {"actor_type": "RepositoryRole", "actor_id": 5, "bypass_mode": "always"}
    )
    v = mirror_guard.ruleset_verdict(rs)
    assert v["healthy"] is False
    assert any("DeployKey-only" in r for r in v["reasons"])


def test_ruleset_wrong_ref_scope_is_drift() -> None:
    rs = _locked_ruleset()
    rs["conditions"]["ref_name"]["include"] = ["refs/heads/**"]
    assert mirror_guard.ruleset_verdict(rs)["healthy"] is False


def test_ruleset_wrong_target_is_drift() -> None:
    rs = _locked_ruleset()
    rs["target"] = "tag"
    assert mirror_guard.ruleset_verdict(rs)["healthy"] is False


# --- Contractual: verdict output schema ------------------------------------
def test_replication_verdict_schema() -> None:
    v = mirror_guard.replication_verdict("a", "a")
    assert set(v) >= {"check", "healthy", "reason", "gerrit_sha", "github_sha"}
    assert (
        v["check"] == "replication"
        and isinstance(v["healthy"], bool)
        and isinstance(v["reason"], str)
    )


def test_ruleset_verdict_schema() -> None:
    v = mirror_guard.ruleset_verdict(_locked_ruleset())
    assert set(v) >= {"check", "healthy", "reason", "reasons"}
    assert (
        v["check"] == "ruleset"
        and isinstance(v["healthy"], bool)
        and isinstance(v["reasons"], list)
    )


# --- Contractual: CLI runner exit codes ------------------------------------
def test_run_all_healthy_exit_0(monkeypatch) -> None:
    monkeypatch.setattr(mirror_guard, "fetch_gerrit_main_sha", lambda *a, **k: "sha1")
    monkeypatch.setattr(mirror_guard, "fetch_github_main_sha", lambda *a, **k: "sha1")
    monkeypatch.setattr(mirror_guard, "fetch_github_ruleset", lambda *a, **k: _locked_ruleset())
    verdicts, code = mirror_guard.run(check_replication=True, check_ruleset=True, github_token="t")
    assert code == 0 and all(v["healthy"] for v in verdicts)


def test_run_drift_exit_1(monkeypatch) -> None:
    monkeypatch.setattr(mirror_guard, "fetch_gerrit_main_sha", lambda *a, **k: "sha1")
    monkeypatch.setattr(mirror_guard, "fetch_github_main_sha", lambda *a, **k: "sha1")
    monkeypatch.setattr(mirror_guard, "fetch_github_ruleset", lambda *a, **k: None)  # deleted
    _verdicts, code = mirror_guard.run(check_replication=True, check_ruleset=True, github_token="t")
    assert code == 1


def test_run_divergence_exit_1(monkeypatch) -> None:
    monkeypatch.setattr(mirror_guard, "fetch_gerrit_main_sha", lambda *a, **k: "new")
    monkeypatch.setattr(mirror_guard, "fetch_github_main_sha", lambda *a, **k: "old")
    _verdicts, code = mirror_guard.run(
        check_replication=True, check_ruleset=False, github_token="t"
    )
    assert code == 1


def test_run_fetch_error_exit_2(monkeypatch) -> None:
    def _boom(*a, **k):
        raise urllib.error.URLError("gerrit unreachable")

    monkeypatch.setattr(mirror_guard, "fetch_gerrit_main_sha", _boom)
    verdicts, code = mirror_guard.run(check_replication=True, check_ruleset=False, github_token="t")
    assert code == 2 and verdicts[-1]["check"] == "io"


# --- Behavioral: I/O fetcher parsing (monkeypatch the _http_get seam) -------
def test_strip_xssi_robust_to_pretty_and_missing_newline() -> None:
    assert mirror_guard._strip_xssi(b')]}\'{"a":1}') == '{"a":1}'  # no newline after prefix
    assert mirror_guard._strip_xssi(b' )]}\'\n {"a": 1}\n') == '{"a": 1}'  # ws + trailing nl
    assert mirror_guard._strip_xssi(b'{"a": 1}') == '{"a": 1}'  # no prefix (plain JSON)


def test_fetch_gerrit_main_sha_strips_xssi_and_extracts_revision(monkeypatch) -> None:
    body = b')]}\'\n{"ref": "refs/heads/main", "revision": "deadbeef"}'
    monkeypatch.setattr(mirror_guard, "_http_get", lambda *a, **k: body)
    assert mirror_guard.fetch_gerrit_main_sha() == "deadbeef"


def test_fetch_github_main_sha_extracts_sha(monkeypatch) -> None:
    monkeypatch.setattr(mirror_guard, "_http_get", lambda *a, **k: b'{"sha": "abc123"}')
    assert mirror_guard.fetch_github_main_sha() == "abc123"


def test_fetch_github_ruleset_lists_then_fetches_detail_by_id(monkeypatch) -> None:
    def fake_get(url, headers=None, timeout=20.0):
        if url.endswith("/rulesets"):
            return b'[{"id": 42, "name": "gerrit-mirror-lock-main"}, {"id": 7, "name": "other"}]'
        assert url.endswith("/rulesets/42")  # detail fetched by the matched id
        return b'{"id": 42, "name": "gerrit-mirror-lock-main", "enforcement": "active"}'

    monkeypatch.setattr(mirror_guard, "_http_get", fake_get)
    rs = mirror_guard.fetch_github_ruleset(token="t")
    assert rs is not None and rs["id"] == 42 and rs["enforcement"] == "active"


def test_fetch_github_ruleset_absent_returns_none(monkeypatch) -> None:
    monkeypatch.setattr(mirror_guard, "_http_get", lambda *a, **k: b'[{"id": 1, "name": "other"}]')
    assert mirror_guard.fetch_github_ruleset(token="t") is None


def test_fetch_github_ruleset_requires_token() -> None:
    with pytest.raises(ValueError):
        mirror_guard.fetch_github_ruleset(token=None)
