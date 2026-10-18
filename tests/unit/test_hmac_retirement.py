"""The legacy HMAC-SHA256 signing scheme is retired (story fuchsia-wettish-leafbird, 2026-09).

Pins the retirement contract against a SNAPSHOT that carries the exact live shape of a
pre-retirement HMAC ``plan-review`` attestation (cf. the folded SNAPSHOTs on the tracker):

  (a) the snapshot still reduces and ``rebar show`` renders the attestation without its hex;
  (b) ``rebar verify-signature --kind plan-review`` reads ``unsigned`` with the retired reason;
  (c) the plan-review status surfaces (library + MCP) report it NOT certified;
  (d) ``signature_findings`` emits nothing for an HMAC-only store;
  (e) ``rebar init`` mints no ``.signing-key`` and ``REBAR_SIGNING_KEY`` changes no verdict;
  (f) no HMAC reader or key-custody symbol remains under ``src/``.
"""

from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import config, signing
from rebar._commands._seam import append_event
from rebar.reducer import reduce_ticket

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KEY_ID = "412fa5372ad877e0"
_RETIRED = "legacy HMAC scheme retired"


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_OPCERT_ENV_ID", raising=False)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(store: Path) -> Path:
    return Path(config.tracker_dir(str(store)))


def _legacy_attestation(ticket_id: str) -> dict:
    """The exact key set of a live pre-retirement HMAC plan-review record."""
    return {
        "algorithm": "HMAC-SHA256",
        "author": "Rebar Bot",
        "head_sha": "2272d68c" + "0" * 32,
        "key_id": _KEY_ID,
        "kind": "plan-review",
        "manifest": [
            "plan-review: PASS",
            f"ticket: {ticket_id}",
            "material: 5e6cba9142ddb7e2",
            "verified-at-sha:2272d68c" + "0" * 32,
        ],
        "signature": "ab" * 32,
        "signed_at": 1783806358702652000,
    }


def _hmac_snapshot_ticket(store: Path) -> str:
    """Create a ticket whose SNAPSHOT folds in a legacy HMAC plan-review attestation."""
    tid = rebar.create_ticket("task", "legacy hmac", repo_root=str(store))
    tracker = _tracker(store)
    resolved = rebar.show_ticket(tid, repo_root=str(store))["ticket_id"]
    tdir = tracker / resolved
    uuids = [json.loads(f.read_text()).get("uuid") for f in tdir.glob("*.json")]
    record = _legacy_attestation(resolved)
    compiled = {
        "ticket_id": resolved,
        "ticket_type": "task",
        "title": "legacy hmac",
        "status": "open",
        "priority": 2,
        "tags": [],
        "attestations": {"plan-review": record},
        "signature": record,
    }
    append_event(
        resolved,
        "SNAPSHOT",
        {"compiled_state": compiled, "source_event_uuids": uuids, "compacted_at": 1},
        tracker,
        repo_root=str(store),
    )
    return resolved


# (a) ─────────────────────────────────────────────────────────────────────────────────
def test_hmac_snapshot_reduces_and_renders_without_hex(store: Path) -> None:
    tid = _hmac_snapshot_ticket(store)
    state = reduce_ticket(str(_tracker(store) / tid))
    assert state["attestations"]["plan-review"]["algorithm"] == "HMAC-SHA256"
    shown = rebar.show_ticket(tid, repo_root=str(store))["attestations"]["plan-review"]
    assert shown["manifest"][0] == "plan-review: PASS"
    assert "signature" not in shown or not shown["signature"]


# (b) ─────────────────────────────────────────────────────────────────────────────────
def test_cli_verify_signature_reads_unsigned_with_retired_reason(store: Path) -> None:
    tid = _hmac_snapshot_ticket(store)
    argv = ["verify-signature", tid, "--kind", "plan-review", "--output", "json"]
    proc = subprocess.run(
        [sys.executable, "-m", "rebar.cli", *argv],
        capture_output=True,
        text=True,
        cwd=str(store),
        check=False,
    )
    assert proc.returncode == 1, proc.stderr
    out = json.loads(proc.stdout)
    assert out["verdict"] == "unsigned" and out["verified"] is False
    assert _RETIRED in out["reason"]
    assert out["key_id"] == _KEY_ID
    assert out["step_count"] == len(_legacy_attestation(tid)["manifest"])


# (c) ─────────────────────────────────────────────────────────────────────────────────
def test_plan_review_status_reports_not_certified(store: Path) -> None:
    from rebar.llm.plan_review.attest_gate import plan_review_status

    tid = _hmac_snapshot_ticket(store)
    status = plan_review_status(tid, repo_root=str(store))
    assert status["ok"] is False
    assert status["verdict"] == "unsigned"


def test_mcp_verify_signature_reads_unsigned(store: Path) -> None:
    pytest.importorskip("mcp")
    from rebar.mcp_server import build_server

    tid = _hmac_snapshot_ticket(store)
    result = asyncio.run(
        build_server().call_tool("verify_signature", {"ticket_id": tid, "kind": "plan-review"})
    )
    structured = result[1] if isinstance(result, tuple) else None
    out = structured if isinstance(structured, dict) else json.loads(result[0].text)
    out = out.get("result", out)
    assert out["verdict"] == "unsigned" and _RETIRED in out["reason"]


# (d) ─────────────────────────────────────────────────────────────────────────────────
def test_signature_findings_silent_for_hmac_only_store(store: Path) -> None:
    from rebar._engine_support.validate import signature_findings

    _hmac_snapshot_ticket(store)
    _hmac_snapshot_ticket(store)
    assert signature_findings(str(_tracker(store))) == []


# (e) ─────────────────────────────────────────────────────────────────────────────────
def test_init_mints_no_signing_key(store: Path) -> None:
    assert not (_tracker(store) / ".signing-key").exists()


def test_injected_signing_key_changes_nothing(store: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tid = _hmac_snapshot_ticket(store)
    before = signing.verify_signature(tid, kind="plan-review", repo_root=str(store))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "x")
    after = signing.verify_signature(tid, kind="plan-review", repo_root=str(store))
    assert before == after and after["verdict"] == "unsigned"


# (f) ─────────────────────────────────────────────────────────────────────────────────
_HMAC_READER = re.compile(
    r"\bsigning_key\(|REBAR_SIGNING_KEY|compute_signature|\bverify_record\b|_signing_hmac"
    r"|_canonical_payload|key_fingerprint|_hmac_opcert_not_certified|_generate_key_file"
)


def test_no_hmac_readers_remain() -> None:
    src = _REPO_ROOT / "src"
    assert not (src / "rebar" / "_signing_hmac.py").exists()
    hits = [
        f"{path.relative_to(_REPO_ROOT)}:{n}"
        for path in sorted(src.rglob("*.py"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _HMAC_READER.search(line)
    ]
    assert hits == [], hits


def test_retired_names_are_gone_from_the_public_surface() -> None:
    for name in ("signing_key", "key_fingerprint", "compute_signature", "verify_record"):
        assert not hasattr(signing, name), name
    assert signing.LEGACY_HMAC_ALGORITHM == "HMAC-SHA256"
    assert signing.HMAC_RETIRED_ON == "2026-09"
