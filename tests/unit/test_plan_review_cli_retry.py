"""Real-CLI end-to-end oracle for ``review-plan --retry`` (story RP-06 S5 — AC8 + AC5).

Unlike ``test_plan_review_retry.py`` (which drives the library entry point in-process), these
tests exercise the flag through the **actual CLI** in a child process against a temporary
ticket store, using the production journal codec and a stateful fake pinned at the model
boundary — exactly the contract the ticket's Testing section mandates. They assert the
observable process contract only: exit code, the stderr remedy, the exact re-run chunk id via
the fake, and the persisted retry lineage on the tickets branch.

Two of these need no fake at all — an ineligible/conflicting invocation is decided BEFORE any
model call, so a plain ``python -m rebar`` subprocess is the faithful oracle and the assertion
that it made zero calls is structural (there is no runner to call). The eligible-resume E2E
runs a small driver that patches the runner-selection seam to inject the counting fake, then
invokes ``rebar._cli.main`` — a real end-to-end CLI dispatch — and reports what the fake saw.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from _subprocess_env import SubprocessEnv, subprocess_env

import rebar
from rebar.llm.plan_review import sidecar

pytestmark = pytest.mark.unit

# Mirrors the library oracle: a budget that runs SOME criteria (leaving a checkpointed
# success to reuse) while F1's chunk is failed on first encounter → a journaled `failed`
# unit that the retry re-runs.
_RETRY_BUDGET = "0.4"
_FAIL_CRITERION = "F1"

_DESC = (
    "A plan body that clears the deterministic readiness floor so the LLM tier runs.\n\n"
    "## What\nchange a thing in `src/thing.py`.\n\n"
    "## Why\nbecause the current behavior is wrong.\n\n"
    "## Acceptance Criteria\n"
    "- [ ] the thing is observably changed\n"
    "- [ ] `pytest tests/unit` proves the change\n"
)

# A driver run as its own process: it patches the runner-selection seam (a lazy import, so
# patching the module attribute is enough — every call site resolves it at call time) to a
# counting fake that fails F1's chunk exactly once, then invokes the REAL CLI entry point.
# It reports the fake's finder-call id-sets and the CLI's exit code as JSON so the parent can
# assert the observable resume contract.
_DRIVER = """
import json, re, sys
from pathlib import Path

import rebar.llm.runner as runner_mod
from rebar.llm.runner import FakeRunner

MODE = sys.argv[3]  # "seed" (fail F1 once) | "retry" (never fail, count re-runs)


class _Fake(FakeRunner):
    name = "fake"

    def __init__(self):
        super().__init__()
        self.finder_calls = []
        self._failed = set()

    def run(self, req):
        from rebar.llm import findings as _f

        schema = req.output_schema
        instructions = req.instructions or ""
        if req.mode == "text":
            return {"text": "[fake]", "runner": self.name, "model": None, "trace_id": None}
        if schema == "plan_review_findings":
            m = re.search(r"\\(ids: ([^)]*)\\)", instructions)
            ids = [s.strip() for s in (m.group(1).split(",") if m else [])]
            if MODE == "seed" and "F1" in ids and "F1" not in self._failed:
                self._failed.add("F1")
                raise RuntimeError("boom F1")
            self.finder_calls.append(ids)
            payload = {"analysis": "", "findings": []}
        elif schema == "plan_review_verification":
            payload = {"verifications": []}
        elif schema == "plan_review_coach":
            payload = {"notes": []}
        else:
            payload = {"analysis": "", "findings": []}
        payload = _f.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


fake = _Fake()


def _select(*a, **k):
    return fake


_orig = runner_mod.get_runner
runner_mod.get_runner = _select
# Modules that already did ``from rebar.llm.runner import get_runner`` captured the original
# binding at import time; rebind those too. Lazily-imported finder modules will instead copy
# the patched source attribute above, so both early and late importers resolve the fake.
for _mod in list(sys.modules.values()):
    if getattr(_mod, "get_runner", None) is _orig:
        setattr(_mod, "get_runner", _select)
import rebar.llm.plan_review.production_batch_runner as _pbr

_pbr.get_runner = _select

import rebar._cli as cli

tid = sys.argv[1]
result_path = Path(sys.argv[2])
argv = ["review-plan", tid] if MODE == "seed" else ["review-plan", tid, "--retry", "--no-sign"]
code = cli.main(argv)
result_path.write_text(
    json.dumps({"exit": code, "finder_calls": fake.finder_calls}), encoding="utf-8"
)
"""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "test@example.com"),
        ("git", "config", "user.name", "Test"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.chdir(repo)
    monkeypatch.delenv("REBAR_USAGE_LOG", raising=False)
    monkeypatch.setenv("REBAR_GATE_SOURCE", "attested")
    monkeypatch.setenv("REBAR_GATE_REF", "HEAD")
    monkeypatch.setenv("REBAR_PLAN_REVIEW_BUDGET", _RETRY_BUDGET)
    rebar.init_repo(repo_root=str(repo))
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


def _child_env(repo: Path) -> SubprocessEnv:
    env = subprocess_env(
        {
            "REBAR_ROOT": str(repo),
            "REBAR_GATE_SOURCE": "attested",
            "REBAR_GATE_REF": "HEAD",
            "REBAR_PLAN_REVIEW_BUDGET": _RETRY_BUDGET,
        }
    )
    env.pop("REBAR_USAGE_LOG", None)
    return env


def _mkticket(repo: Path) -> str:
    desc = _DESC + f"\nMaterial nonce: {uuid.uuid4().hex}.\n"
    tid = rebar.create_ticket("task", "retry cli fixture", description=desc, repo_root=str(repo))
    rebar.set_file_impact(tid, [{"path": "src/thing.py", "reason": "c"}], repo_root=str(repo))
    return tid


def _sidecar_files(repo: Path, tid: str) -> list[Path]:
    ticket_dir = repo / ".tickets-tracker" / tid
    return sorted(ticket_dir.glob(f"*-{sidecar.EVENT_TYPE}.json"))


# ── AC8: eligible resume, end to end through the real CLI ───────────────────────────
def test_cli_retry_resumes_and_persists_lineage(repo: Path, tmp_path: Path) -> None:
    """Seed AND retry BOTH run through the real CLI so the checkpoint cache (which, under
    the ``attested`` gate source, lives inside the per-sha review snapshot) has consistent
    residency across the two invocations — the seed's checkpointed successes are exactly
    what the retry resumes."""
    tid = _mkticket(repo)
    driver = tmp_path / "retry_driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")

    def _run(mode: str, out: Path) -> dict:
        proc = subprocess.run(
            [sys.executable, str(driver), tid, str(out), mode],
            cwd=repo,
            env=_child_env(repo),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{mode} driver crashed:\n{proc.stderr}"
        return json.loads(out.read_text(encoding="utf-8"))

    # Seed the eligible INDETERMINATE (a journaled `failed` F1 unit) through the CLI.
    seed = _run("seed", tmp_path / "seed.json")
    assert seed["exit"] == 2  # INDETERMINATE (a failed unit + shed criteria)
    seed_payload = sidecar.latest_review_result(tid, repo_root=str(repo))
    assert seed_payload["verdict"] == "INDETERMINATE"
    assert any(u["kind"] == "failed" for u in seed_payload["discovery_journal"]["units"])
    before = len(_sidecar_files(repo, tid))

    # Resume ONLY the latest review through the CLI.
    result = _run("retry", tmp_path / "result.json")

    # INDETERMINATE (shed units stay shed) → the plan-review non-runnable exit code.
    assert result["exit"] == 2
    # Exactly ONE finder call — the previously-failed F1 chunk; all reused successes made
    # ZERO calls (checkpoint resume).
    flat = [i for call in result["finder_calls"] for i in call]
    assert result["finder_calls"] and _FAIL_CRITERION in flat
    assert sum(_FAIL_CRITERION in call for call in result["finder_calls"]) == 1

    # The retry persisted a NEW sidecar carrying versioned retry lineage.
    assert len(_sidecar_files(repo, tid)) == before + 1
    payload = sidecar.latest_review_result(tid, repo_root=str(repo))
    lineage = payload.get("retry_lineage")
    assert lineage is not None
    assert lineage["version"] == sidecar.RETRY_LINEAGE_VERSION
    assert lineage["attempts"] >= 1


# ── AC8: zero-call refusal, end to end through the real CLI ─────────────────────────
def test_cli_retry_refuses_unreviewed_ticket_with_zero_calls(repo: Path) -> None:
    """A ticket that was never reviewed has no retryable latest result: the CLI refuses
    before any model call (there is no runner to call), exits 2, prints the full-review
    remedy on stderr, and writes NO sidecar."""
    tid = _mkticket(repo)
    assert _sidecar_files(repo, tid) == []

    proc = subprocess.run(
        [sys.executable, "-m", "rebar", "review-plan", tid, "--retry"],
        cwd=repo,
        env=_child_env(repo),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "review-plan" in proc.stderr.lower()
    # The remedy points back at a normal full review.
    assert "retry" in proc.stderr.lower()
    assert _sidecar_files(repo, tid) == []


# ── AC5: --retry is mutually exclusive with --force / --status / --check ─────────────
@pytest.mark.parametrize("other", ["--force", "--status", "--check"])
def test_cli_retry_conflicts_are_rejected(repo: Path, other: str) -> None:
    """The conflicting combination is rejected by argument validation (exit 2) before any
    review work — a dummy ticket id is enough because validation precedes dispatch."""
    proc = subprocess.run(
        [sys.executable, "-m", "rebar", "review-plan", "0000-0000", "--retry", other],
        cwd=repo,
        env=_child_env(repo),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "--retry" in proc.stderr


def test_cli_retry_is_compatible_with_no_sign(repo: Path) -> None:
    """``--retry --no-sign`` is NOT rejected by validation: the pair is accepted and fails
    only later on eligibility (exit 2 refusal), never on a flag conflict."""
    tid = _mkticket(repo)
    proc = subprocess.run(
        [sys.executable, "-m", "rebar", "review-plan", tid, "--retry", "--no-sign"],
        cwd=repo,
        env=_child_env(repo),
        capture_output=True,
        text=True,
    )
    # Refused on eligibility (never reviewed), not on a flag conflict.
    assert proc.returncode == 2
    assert "not allowed with" not in proc.stderr
    assert "--no-sign" not in proc.stderr
