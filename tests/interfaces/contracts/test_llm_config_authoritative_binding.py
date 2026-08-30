"""Interface oracle for RP-04 S3 — making the LLM ``OperationSnapshot`` (a focused
``LLMConfig`` projection) AUTHORITATIVE for LLM/gate/workflow operations, completing
ADR 0098 for the LLM surface (ticket ec44-b572-1067-4c52, the LLM-side counterpart
of 3a08's ``test_operation_snapshot_authoritative_binding.py``).

This mirrors 3a08's oracle shape but for :func:`rebar.llm.config_binding.
compose_and_bind_llm_config` rather than the general operation snapshot:

AC1: each public LLM/gate/workflow operation (``review_code``, ``verify_completion``,
``review_plan``, ``scan_epics_for_spec``, ``run_workflow``) composes exactly ONE
``LLMConfig``, reused (never recomposed) by nested subcalls/steps within the same
operation. ``resign_plan_review`` is EXCLUDED (documented no-LLM exception, see
its own docstring) — it must bind NO ``LLMConfig`` at all.

AC2 (metamorphic): mutating model/provider/timeout/retry/cache/headers/tracing/
repository settings AFTER composition cannot change an in-progress operation's
bound config; a fresh operation observes the mutation.

AC3 (failure edge): missing/conflicting decision-bearing provider/auth composition
fails BEFORE any external call, with no anonymous/cross-provider fallback — unlike
the general operation snapshot's fail-OPEN swallow, this composer propagates.

AC4 (secret boundary): no secret/live capability (``api_key``, ``ticket_view``, ...)
ever enters the redacted snapshot values or its fingerprint.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rebar.llm.config import LLMConfig, _active_gate_config
from rebar.llm.config_binding import (
    compose_and_bind_llm_config,
    llm_config_fingerprint,
    redacted_snapshot_values,
)

pytestmark = pytest.mark.unit


# ── AC1: compose exactly once per operation, reused by nested seams ───────────
def test_nested_compose_and_bind_reuses_the_outer_config(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[LLMConfig] = []
    real_from_env = LLMConfig.from_env

    def _counting_from_env(*, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        calls.append(cfg)
        return cfg

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_counting_from_env))
    with compose_and_bind_llm_config() as outer:
        with compose_and_bind_llm_config() as inner:
            assert inner is outer  # reused, not recomposed
    assert len(calls) == 1, "a nested call must not recompose a second LLMConfig"


def test_no_binding_outside_any_operation() -> None:
    assert _active_gate_config.get() is None


def test_explicit_config_is_always_authoritative_and_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit ``config=`` is bound (not merely returned) so nested subcalls observe
    the SAME instance, even when an outer ``LLMConfig.from_env()`` would differ."""
    monkeypatch.setenv("REBAR_LLM_TIMEOUT", "111")
    explicit = LLMConfig(timeout_s=999)
    with compose_and_bind_llm_config(explicit=explicit) as bound:
        assert bound is explicit
        assert _active_gate_config.get() is explicit
        # a nested (no-explicit) call reuses the explicit binding, not the env value
        with compose_and_bind_llm_config() as inner:
            assert inner is explicit
    assert _active_gate_config.get() is None


# ── AC2: settings are frozen for the duration of a bound operation ───────────
@pytest.mark.parametrize(
    "env_name",
    [
        "REBAR_LLM_MODEL_PROVIDER",
        "REBAR_LLM_TIMEOUT",
        "REBAR_LLM_RETRY_MAX_ATTEMPTS",
        "REBAR_LLM_MAX_TOKENS",
    ],
)
def test_mid_operation_env_mutation_does_not_change_the_bound_config(
    env_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    with compose_and_bind_llm_config() as cfg:
        before = redacted_snapshot_values(cfg)
        # Mutate the ambient env AFTER composition — a value this LLMConfig field
        # would read on a fresh `from_env()` call.
        monkeypatch.setenv(env_name, "definitely-a-different-value-999")
        still_bound = _active_gate_config.get()
        assert still_bound is cfg  # the SAME instance, not recomposed
        assert redacted_snapshot_values(still_bound) == before

    # outside the operation, a fresh composition observes an independently-resolved config
    # (never raises simply because the env changed; may differ from `before`).
    with compose_and_bind_llm_config() as after:
        assert after is not cfg


def test_mid_run_mutation_isolated_then_observed_by_the_next_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The full metamorphic shape: bind, observe a value, mutate every behavior-bearing
    knob mid-run, prove the ACTIVE run is unaffected, then prove the NEXT (unbound)
    composition observes every mutation."""
    with compose_and_bind_llm_config() as run_cfg:
        original = redacted_snapshot_values(run_cfg)
        monkeypatch.setenv("REBAR_LLM_MODEL_PROVIDER", "bedrock-mutated")
        monkeypatch.setenv("REBAR_LLM_TIMEOUT", "7")
        monkeypatch.setenv("REBAR_LLM_RETRY_MAX_ATTEMPTS", "1")
        monkeypatch.setenv("REBAR_LLM_MAX_TOKENS", "42")
        monkeypatch.setenv("REBAR_LLM_HEADERS", '{"X-Test": "mutated"}')
        # still mid-operation: the bound instance is untouched.
        assert redacted_snapshot_values(_active_gate_config.get()) == original

    # a fresh, unbound composition after the operation ends sees the mutated environment.
    with compose_and_bind_llm_config() as next_cfg:
        after = redacted_snapshot_values(next_cfg)
    assert after["model_provider"] != original["model_provider"]
    assert after["timeout_s"] != original["timeout_s"]


# ── AC3: fail-fast, no anonymous/cross-provider fallback ─────────────────────
def test_composition_failure_propagates_uncaught(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unlike ``compose_and_bind_operation_snapshot`` (fail-OPEN), a broken LLM config
    must fail BEFORE any external call — no swallow, no degrade to unbound ambient
    resolution."""

    class _Boom(Exception):
        pass

    def _raise(*, repo_root=None):
        raise _Boom("simulated unresolvable auth/provider composition")

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_raise))
    with pytest.raises(_Boom):
        with compose_and_bind_llm_config():
            pytest.fail("must never enter the block on a composition failure")
    # no binding is left dangling after the failed composition.
    assert _active_gate_config.get() is None


def test_composition_failure_leaves_no_runner_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    """A concrete demonstration that the fail-fast happens strictly before any call that
    could reach a runner/provider: a spy runner records if it was ever constructed/used."""
    calls: list[str] = []

    class _Boom(Exception):
        pass

    def _raise(*, repo_root=None):
        calls.append("from_env")
        raise _Boom("missing/conflicting provider auth")

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_raise))
    with pytest.raises(_Boom):
        with compose_and_bind_llm_config():
            calls.append("runner.run")  # would only execute post-composition
    assert calls == ["from_env"]  # composition attempted once; the body never ran


# ── AC4: no secret / live capability ever enters the redacted projection ─────
def test_redacted_snapshot_values_excludes_secret_and_live_fields() -> None:
    cfg = LLMConfig(runner="fake", model="m", api_key="sk-super-secret-value")
    values = redacted_snapshot_values(cfg)
    assert "api_key" not in values
    assert "ticket_view" not in values
    for v in values.values():
        assert "sk-super-secret-value" not in repr(v)


def test_redacted_snapshot_values_only_exposes_header_and_mcp_server_names() -> None:
    """Header/MCP-server VALUES may carry resolved secrets (the ``${env:...}``/
    ``${run:...}`` substitution grammar); only their KEY NAMES are ever exposed."""
    cfg = LLMConfig(
        runner="fake",
        headers={"Authorization": "Bearer sk-secret-token-value"},
        mcp_servers={"svc": {"api_key": "another-secret-value"}},
    )
    values = redacted_snapshot_values(cfg)
    assert values["header_names"] == ["Authorization"]
    assert values["mcp_server_names"] == ["svc"]
    blob = repr(values)
    assert "sk-secret-token-value" not in blob
    assert "another-secret-value" not in blob


def test_llm_config_fingerprint_is_stable_and_secret_free(tmp_path: Path) -> None:
    cfg = LLMConfig(runner="fake", model="m", api_key="sk-should-never-appear")
    fp1 = llm_config_fingerprint(cfg, repo_root=str(tmp_path))
    fp2 = llm_config_fingerprint(cfg, repo_root=str(tmp_path))
    assert fp1 == fp2  # stable content hash, not a fresh random id
    assert "sk-should-never-appear" not in fp1


def test_llm_config_fingerprint_rejects_non_json_primitive_leaf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fingerprint reuses ``OperationSnapshot.build``'s validating constructor, so a
    live/non-JSON-primitive object injected into the allowlisted projection is rejected
    rather than silently serialized (or crashing opaquely)."""
    import rebar.llm.config_binding as config_binding

    def _leaky_values(cfg: LLMConfig) -> dict[str, object]:
        return {"not_json_safe": object()}

    monkeypatch.setattr(config_binding, "redacted_snapshot_values", _leaky_values)
    cfg = LLMConfig(runner="fake")
    with pytest.raises(Exception):  # noqa: B017 — OperationSnapshot's own validation error type
        config_binding.llm_config_fingerprint(cfg, repo_root=str(os.getcwd()))


# ── resign_plan_review: documented exclusion — no LLM call, no LLMConfig bound ──
def test_resign_plan_review_binds_no_llm_config(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``resign_plan_review`` makes NO LLM call (its own docstring: "NO LLM and NO
    network") and must not be classified alongside the four LLM-composing entry
    points — proven here by asserting ``LLMConfig.from_env`` is never invoked and no
    ``LLMConfig`` is ever bound while it runs."""
    import rebar
    from rebar.llm.plan_review.resign import resign_plan_review

    def _fail_if_called(*, repo_root=None):
        pytest.fail("resign_plan_review must never compose an LLMConfig")

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_fail_if_called))
    tid = rebar.create_ticket(
        "task", "resign target", description="body", repo_root=str(rebar_repo)
    )
    result = resign_plan_review(tid, repo_root=str(rebar_repo))
    assert result["ok"] is False  # no sidecar yet — correctly refused, not an LLM call
    assert _active_gate_config.get() is None


# ── through-entry-point: compose-once wired via real production entry points ──
def test_review_code_composes_the_llm_config_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``review_code`` runs a four-pass gate (multiple ``RunRequest``s), yet must
    compose exactly ONE ``LLMConfig`` for the whole operation (AC1)."""
    from rebar.llm.code_review import detectors as _det
    from rebar.llm.code_review import review_code
    from rebar.llm.runner import FakeRunner

    monkeypatch.setenv("REBAR_GATE_SOURCE", "local")
    monkeypatch.delenv("REBAR_GATE_REF", raising=False)
    monkeypatch.setattr(_det, "run_security_detectors", lambda **kw: {})

    calls: list[LLMConfig] = []
    real_from_env = LLMConfig.from_env

    def _counting_from_env(*, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        calls.append(cfg)
        return cfg

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_counting_from_env))
    structured = {
        "findings": [],
        "recommend_overlays": [],
        "verifications": [],
        "notes": [],
        "summary": "x",
    }
    runner = FakeRunner(structured=structured)
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('hi')\n"
    review_code(diff_text=diff, changed_files=["x.py"], runner=runner)
    assert len(calls) == 1, "review_code's four-pass gate must reuse ONE composed LLMConfig"


def test_run_workflow_composes_the_llm_config_exactly_once_across_steps(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A multi-step LLM workflow run must compose exactly ONE ``LLMConfig`` for the
    whole run — the compose-once binding now lives in ``workflow/runs.py::run()``
    itself (AC1), reused by every agent step regardless of how many there are."""
    import rebar
    from rebar.llm.runner import FakeRunner
    from rebar.llm.workflow import runs

    r = str(rebar_repo)
    calls: list[LLMConfig] = []
    real_from_env = LLMConfig.from_env

    def _counting_from_env(*, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        calls.append(cfg)
        return cfg

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_counting_from_env))

    tid = rebar.create_ticket("task", "WF", description="body", repo_root=r)
    pdir = Path(r) / ".rebar" / "prompts"
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "compose-once.md").write_text(
        "---\noutputs: findings\n---\nReview {{ticket_id}}.", encoding="utf-8"
    )
    doc = {
        "schema_version": "1",
        "name": "compose_once_demo",
        "steps": [
            {"id": "pass1", "prompt": "compose-once", "with": {"ticket_id": tid, "context": "c"}},
            {
                "id": "pass2",
                "prompt": "compose-once",
                "needs": ["pass1"],
                "with": {"ticket_id": tid, "context": "c"},
            },
        ],
    }
    canned = {"findings": [], "summary": "ok"}
    res = runs.run(
        doc,
        {},
        repo_root=r,
        source_mode="local",
        review_runner=FakeRunner(structured=canned),
    )
    assert res["status"] == "succeeded", res
    assert len(calls) == 1, "two agent steps in one run must share ONE composed LLMConfig"


def test_verify_completion_composes_the_llm_config_exactly_once(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``verify_completion`` (``_run_completion_at_handle``) must compose exactly ONE
    ``LLMConfig`` for its verifier run (AC1)."""
    import rebar
    from rebar.llm.runner import FakeRunner

    r = str(rebar_repo)
    calls: list[LLMConfig] = []
    real_from_env = LLMConfig.from_env

    def _counting_from_env(*, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        calls.append(cfg)
        return cfg

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_counting_from_env))
    tid = rebar.create_ticket(
        "task",
        "verify me",
        description="A task with criteria.\n\n## Acceptance Criteria\n- [ ] the thing exists\n",
        repo_root=r,
    )
    structured = {"verdict": "PASS", "findings": [], "summary": "ok"}
    rebar.llm.verify_completion(
        tid, graph=False, repo_root=r, runner=FakeRunner(structured=structured)
    )
    assert len(calls) == 1, "verify_completion must compose exactly ONE LLMConfig"


def test_review_plan_composes_the_llm_config_exactly_once(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``review_plan`` runs a find -> verify -> decide + coach multi-pass gate (multiple
    ``RunRequest``s), yet must compose exactly ONE ``LLMConfig`` for the whole operation
    (AC1) — the entry point newly wired to ``compose_and_bind_llm_config`` in this
    ticket, alongside ``review_code``/``verify_completion``/``run_workflow`` above."""
    import subprocess

    import rebar
    from rebar.llm import findings as _f
    from rebar.llm.runner import FakeRunner

    r = str(rebar_repo)
    # the local-mode SHA fallback reads the committed HEAD (mirrors test_block_loop_
    # remediation.py's fixture)
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"], cwd=r, check=True, capture_output=True
    )

    class _GateFake(FakeRunner):
        """Shape-valid offline runner: empty finder output (so no findings need
        verification), empty coach notes — a PASS that still exercises Pass-1 and
        Pass-4 as separate RunRequests within the one operation."""

        name = "fake"

        def run(self, req) -> dict:  # type: ignore[override]
            schema = req.output_schema
            if req.mode == "text":
                return {
                    "text": "[fake summary]",
                    "runner": self.name,
                    "model": None,
                    "trace_id": None,
                }
            payload = (
                {"notes": []} if schema == "plan_review_coach" else {"analysis": "", "findings": []}
            )
            payload = _f.validate_structured(dict(payload), schema)
            return {**payload, "runner": self.name, "model": None, "trace_id": None}

    calls: list[LLMConfig] = []
    real_from_env = LLMConfig.from_env

    def _counting_from_env(*, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        calls.append(cfg)
        return cfg

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_counting_from_env))
    tid = rebar.create_ticket(
        "task",
        "compose-once plan",
        description=(
            "A plan body that clears the deterministic readiness floor so the LLM tier "
            "runs, exercising the multi-pass gate the compose-once binding covers.\n\n"
            "## What\nchange a thing in `src/thing.py`.\n\n"
            "## Why\nbecause the current behavior is wrong.\n\n"
            "## Acceptance Criteria\n"
            "- [ ] the thing is observably changed\n"
            "- [ ] `pytest tests/unit` proves the change\n"
        ),
        repo_root=r,
    )
    verdict = rebar.llm.review_plan(tid, runner=_GateFake(), repo_root=r)
    assert verdict["verdict"] == "PASS", verdict
    assert len(calls) == 1, "review_plan's multi-pass gate must reuse ONE composed LLMConfig"


def test_scan_epics_for_spec_composes_the_llm_config_exactly_once_across_batches(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``scan_epics_for_spec`` batches candidate epics into multiple runner passes
    (one per batch), yet must compose exactly ONE ``LLMConfig`` for the whole operation
    (AC1) — the entry point newly wired to ``compose_and_bind_llm_config`` in this
    ticket."""
    import rebar
    from rebar.llm.runner import FakeRunner

    r = str(rebar_repo)
    for i in range(3):
        rebar.create_ticket(
            "epic",
            f"Epic {i}",
            description=f"Body {i}.\n\n## Acceptance Criteria\n- [ ] x",
            repo_root=r,
        )

    calls: list[LLMConfig] = []
    real_from_env = LLMConfig.from_env

    def _counting_from_env(*, repo_root=None):
        cfg = real_from_env(repo_root=repo_root)
        calls.append(cfg)
        return cfg

    monkeypatch.setattr(LLMConfig, "from_env", staticmethod(_counting_from_env))
    result = rebar.llm.scan_epics_for_spec(
        "The system must do X and Y.",
        batch_size=2,  # 3 epics @ batch_size 2 -> 2 batches -> 2 RunRequests
        runner=FakeRunner(findings=[]),
        repo_root=r,
    )
    assert len(result["target"]["ticket_ids"]) == 3
    assert len(calls) == 1, (
        "scan_epics_for_spec's per-batch passes must reuse ONE composed LLMConfig"
    )
