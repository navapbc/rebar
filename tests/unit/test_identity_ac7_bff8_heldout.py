"""HELD-OUT oracle for AC7 (bff8) — the implementation MUST NOT see this file.

The enforcement-gate behaviour the happy path can't cover: the verify-authorship alias,
`--since` grandfathering (pre-cutover events don't fail; post-cutover do), the
`--format json` report shape validated against verify_identity_report.schema.json, and the
CI merge-gate workflow file.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "dev@example.com"),
        ("git", "config", "user.name", "Dev"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=r, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "REBAR_ROOT": str(repo), "REBAR_IDENTITY_REQUIRE_AUTHENTICATED": "0"}
    return subprocess.run(["rebar", *args], cwd=repo, env=env, capture_output=True, text=True)


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(tracker_dir(str(repo))),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


# ── alias ─────────────────────────────────────────────────────────────────────
def test_verify_authorship_alias_still_works(repo: Path) -> None:
    """The delivered `verify-authorship` name still dispatches the same gate (alias)."""
    rebar.create_ticket("task", "t", repo_root=str(repo))
    res = _run(repo, "verify-authorship", "--all")
    assert res.returncode == 0
    both = _run(repo, "verify-identity", "--all")
    assert both.returncode == 0


# ── --since grandfathering ────────────────────────────────────────────────────
def test_pre_cutover_event_is_grandfathered(repo: Path) -> None:
    """An unsigned event introduced BEFORE the cutover ref does not fail the gate."""
    rebar.create_ticket("task", "old unsigned", repo_root=str(repo))  # pre-cutover event
    # Advance the tickets branch with a GATE-EXEMPT identity so the cutover commit itself
    # carries no enforceable work event (only the pre-cutover task is in scope).
    rebar.create_identity("Cutover Marker", "cut@example.com", repo_root=str(repo))
    cutover = _head(repo)  # cutover ref = after the old event

    # With enforcement + --since cutover, the pre-cutover unsigned event is grandfathered.
    res = _run(repo, "verify-identity", "--all", "--require-authenticated", "--since", cutover)
    assert res.returncode == 0, res.stdout + res.stderr
    # Without --since, the same unsigned event IS enforced → non-zero.
    res_no = _run(repo, "verify-identity", "--all", "--require-authenticated")
    assert res_no.returncode != 0, res_no.stdout + res_no.stderr


def test_post_cutover_event_is_enforced(repo: Path) -> None:
    """An unsigned event introduced AFTER the cutover ref fails the gate."""
    rebar.create_identity("Cutover Marker", "cut@example.com", repo_root=str(repo))
    cutover = _head(repo)
    rebar.create_ticket("task", "new unsigned", repo_root=str(repo))  # post-cutover event

    res = _run(repo, "verify-identity", "--all", "--require-authenticated", "--since", cutover)
    assert res.returncode != 0, res.stdout + res.stderr


# ── --format json report ──────────────────────────────────────────────────────
def test_json_report_shape_and_schema(repo: Path) -> None:
    """`--format json` emits report entries with the AC-specified fields, validating
    against verify_identity_report.schema.json."""
    rebar.create_ticket("task", "unsigned", repo_root=str(repo))
    res = _run(repo, "verify-identity", "--all", "--require-authenticated", "--format", "json")
    # The command may exit non-zero (unsigned event) but must still emit valid JSON on stdout.
    payload = json.loads(res.stdout)
    assert isinstance(payload, list) and payload, "expected a non-empty JSON report array"
    entry = payload[0]
    assert set(entry.keys()) >= {
        "event_uuid",
        "ticket_id",
        "commit",
        "author",
        "verdict",
        "display",
        "grandfathered",
    }
    assert entry["display"] in {"verified", "unverified", "unsigned"}

    # Validate every entry against the shipped report schema.
    import jsonschema  # noqa: PLC0415 — test-only dependency

    schema_path = (
        Path(rebar.__file__).resolve().parent / "schemas" / "verify_identity_report.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    for e in payload:
        jsonschema.validate(e, schema)


# ── CI merge-gate workflow file ───────────────────────────────────────────────
def test_ci_merge_gate_workflow_exists() -> None:
    """A CI workflow under .github/workflows/ invokes the verify-identity merge-gate."""
    root = Path(rebar.__file__).resolve().parent.parent.parent  # repo root
    wf_dir = root / ".github" / "workflows"
    hits = [p for p in wf_dir.glob("*.y*ml") if "verify-identity" in p.read_text(encoding="utf-8")]
    assert hits, "expected a .github/workflows/*.yaml invoking `rebar verify-identity`"
