"""Held-out contracts for the gate ERROR sidecar (ticket 8bc5). WITHHELD.

- the COMPLETION gate's infra path writes the gate_error record AND still re-raises
  (fail-closed) — the asymmetry vs plan-review's write-then-degrade,
- the existing schema-guarded verdict readers never surface the gate_error record
  (so ERROR never pollutes verdict reads),
- the ERROR verdict survives as "ERROR" (it is NOT coerced to FAIL by reconcile_verdict),
- a NON-infra exception writes no gate_error record (the record is gated to infra outages).
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
    name = "outage"

    def preflight(self) -> None:
        raise LLMUnavailableError("simulated systemic provider outage")

    def run(self, req):  # noqa: ANN001, ANN201
        raise LLMUnavailableError("simulated systemic provider outage")


class _BrokenRunner:
    """Raises a NON-infra exception — must NOT be recorded as a gate error."""

    name = "broken"

    def preflight(self) -> None:
        raise RuntimeError("a plain bug, not an infra outage")

    def run(self, req):  # noqa: ANN001, ANN201
        raise RuntimeError("a plain bug, not an infra outage")


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


def _scan(ticket_id: str, repo_root: str, suffix: str) -> list[dict]:
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
    return [p for p in _scan(ticket_id, repo_root, suffix) if p.get("schema") == "gate_error_v1"]


def _new_ticket(store: str) -> str:
    return rebar.create_ticket(
        "task",
        "work ticket",
        description="A well-formed ticket.\n\n## Acceptance Criteria\n- [ ] x",
        repo_root=store,
    )


def test_completion_gate_outage_records_error_and_reraises(store):
    from rebar.llm.workflow import gate_dispatch

    tid = _new_ticket(store)
    # The completion gate FAIL-CLOSES: it must re-raise the outage...
    with pytest.raises(LLMUnavailableError):
        gate_dispatch.produce_completion_verdict(
            tid,
            graph=False,
            repo_root=store,
            cfg=LLMConfig.from_env(repo_root=store),
            runner=_OutageRunner(),
        )
    # ...AND still have persisted a gate_error_v1 record before propagating.
    errs = _gate_errors(tid, store, "COMPLETION_VERDICT")
    assert errs, "completion-gate outage must persist a gate_error_v1 record before re-raising"
    assert errs[0]["verdict"] == "ERROR"


def test_existing_verdict_readers_skip_gate_error_record(store):
    from rebar.llm.plan_review import review_plan
    from rebar.llm.plan_review import sidecar as pr_sidecar

    tid = _new_ticket(store)
    review_plan(
        tid,
        source="local",
        repo_root=store,
        config=LLMConfig.from_env(repo_root=store),
        runner=_OutageRunner(),
        sign=False,
        emit_sidecar=True,
    )
    # The schema-guarded verdict reader must NOT surface the gate_error_v1 record
    # (it guards plan_review_result_v1/v2 only) — so ERROR never pollutes verdict reads.
    latest = pr_sidecar.latest_review_result(tid, repo_root=store)
    if latest is not None:
        assert latest.get("schema") != "gate_error_v1"
        assert latest.get("verdict") != "ERROR"


def test_gate_error_verdict_not_coerced_to_fail(store):
    # The dedicated ERROR builder must NOT route through reconcile_verdict (which
    # coerces any non-PASS to FAIL) — the persisted verdict stays "ERROR".
    from rebar.llm.plan_review import review_plan

    tid = _new_ticket(store)
    review_plan(
        tid,
        source="local",
        repo_root=store,
        config=LLMConfig.from_env(repo_root=store),
        runner=_OutageRunner(),
        sign=False,
        emit_sidecar=True,
    )
    errs = _gate_errors(tid, store, "REVIEW_RESULT")
    assert errs
    assert errs[0]["verdict"] == "ERROR"
    assert errs[0]["verdict"] != "FAIL"


def test_non_infra_exception_writes_no_gate_error(store):
    # Only an infra outage (LLMUnavailableError) produces a gate_error record; a plain
    # bug (any other exception) must not be mislabeled as an infra diagnosis interval.
    from rebar.llm.plan_review import review_plan

    tid = _new_ticket(store)
    try:
        review_plan(
            tid,
            source="local",
            repo_root=store,
            config=LLMConfig.from_env(repo_root=store),
            runner=_BrokenRunner(),
            sign=False,
            emit_sidecar=True,
        )
    except Exception:  # noqa: BLE001 — a non-infra failure may surface however the gate handles it
        pass
    assert not _gate_errors(tid, store, "REVIEW_RESULT"), (
        "a non-infra exception must not write a gate_error record"
    )
