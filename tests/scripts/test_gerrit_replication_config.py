"""Gerrit replication config invariants."""

from pathlib import Path

REPLICATION_CONFIG = Path(__file__).resolve().parents[2] / "infra" / "gerrit" / "replication.config"


def _github_remote_body() -> str:
    text = REPLICATION_CONFIG.read_text()
    return text.split('[remote "github"]', 1)[1].split("[replication]", 1)[0]


def _push_specs() -> list[str]:
    specs: list[str] = []
    for line in _github_remote_body().splitlines():
        stripped = line.strip()
        if stripped.startswith("push = "):
            specs.append(stripped.removeprefix("push = "))
    return specs


def test_change_refs_are_forced_without_widening_replication_scope() -> None:
    body = _github_remote_body()
    specs = _push_specs()

    assert "+refs/changes/*:refs/changes/*" in specs
    assert "refs/changes/*:refs/changes/*" not in specs
    assert "refs/heads/main:refs/heads/main" in specs
    assert "refs/tags/*:refs/tags/*" in specs
    assert "refs/heads/feature/*:refs/heads/feature/*" in specs
    assert "refs/*:refs/*" not in specs
    assert [spec for spec in specs if spec.startswith("+")] == ["+refs/changes/*:refs/changes/*"]

    assert "projects = rebar" in body
    assert "mirror = false" in body
    assert "replicatePermissions = false" in body
