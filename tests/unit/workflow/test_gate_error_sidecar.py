"""Happy-path contract for the gate ERROR sidecar (ticket 8bc5).

Tier: unit (real store + injected infra outage; no network — the runner is stubbed
to raise ``LLMUnavailableError``, so ``get_runner`` is never called). This pins the
core new behavior: when a gate hits an infrastructure exception, a dedicated
``gate_error_v1`` sidecar record (verdict ``ERROR`` with a non-empty ``error.cause``)
is persisted — ADDITIVELY, without changing the gate's existing outcome (plan-review
still degrades to INDETERMINATE). Completion-path / reader-isolation / no-false-positive
contracts live in the held-out companion.
"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

import rebar
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError

pytestmark = pytest.mark.unit


class _OutageRunner:
    """A runner whose infra calls raise LLMUnavailableError — drives the gate's
    ``except LLMUnavailableError`` (infra) path without any network."""

    name = "outage"

    def preflight(self) -> None:
        raise LLMUnavailableError("simulated systemic provider outage")

    def run(self, req):  # noqa: ANN001, ANN201
        raise LLMUnavailableError("simulated systemic provider outage")


@pytest.fixture
def store(tmp_path, monkeypatch):
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
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    return str(repo)


def scan_event_payloads(ticket_id: str, repo_root: str, suffix: str) -> list[dict]:
    """Raw sidecar ``data`` payloads for a ticket (bypasses the schema-guarded
    verdict readers, so a gate_error_v1 record is visible)."""
    from rebar import config as _config
    from rebar._engine_support.resolver import resolve_ticket_dir_name

    tracker = str(_config.tracker_dir(repo_root))
    ticket_dir = os.path.join(tracker, resolve_ticket_dir_name(ticket_id, tracker))
    out = []
    for f in sorted(os.listdir(ticket_dir)):
        if f.endswith(f"-{suffix}.json") and not f.startswith("."):
            with open(os.path.join(ticket_dir, f), encoding="utf-8") as fh:
                out.append(json.load(fh)["data"])
    return out


def _gate_errors(ticket_id: str, repo_root: str, suffix: str) -> list[dict]:
    return [
        p
        for p in scan_event_payloads(ticket_id, repo_root, suffix)
        if p.get("schema") == "gate_error_v1"
    ]


def test_plan_review_outage_writes_gate_error_and_still_degrades(store):
    tid = rebar.create_ticket(
        "task",
        "work ticket",
        description="A well-formed ticket.\n\n## Acceptance Criteria\n- [ ] x",
        repo_root=store,
    )

    from rebar.llm.plan_review import review_plan

    verdict = review_plan(
        tid,
        source="local",
        repo_root=store,
        config=LLMConfig.from_env(repo_root=store),
        runner=_OutageRunner(),
        sign=False,
        emit_sidecar=True,
    )

    # 1) The pre-existing plan-review outcome is preserved: soft-degrade to INDETERMINATE.
    assert verdict["verdict"] == "INDETERMINATE"
    assert verdict["coverage"]["llm_unavailable"] is True

    # 2) A dedicated gate_error_v1 record is persisted on the REVIEW_RESULT stream.
    errs = _gate_errors(tid, store, "REVIEW_RESULT")
    assert errs, "an infra outage at the plan-review gate must persist a gate_error_v1 record"
    rec = errs[0]
    assert rec["verdict"] == "ERROR"
    assert rec.get("error", {}).get("cause"), "gate_error record must carry a non-empty error.cause"
