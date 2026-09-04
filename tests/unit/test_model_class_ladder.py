"""Contract tests for task 7761 — the Pass-1 ladder expressed in MODEL CLASSES.

The ladder in ``workflow/gates/plan-review.yaml`` held BARE Anthropic ids, and three separate
consumers copy ``model_ladder[0]`` onto the model a run actually uses. So a run configured for
another provider still sent Pass-1 — 41 of 42 calls on a real review — to DIRECT ANTHROPIC.

THE OBSERVABLE in every test here is the model id a ladder consumer ACTUALLY USES, never the
resolver in isolation (``resolve_model_string`` is f844's and is already tested at
``test_model_classes.py:329``):

* :class:`ProductionBatchRunner` — the ``cfg.model`` handed to ``run_pass1``;
* :class:`DefaultBatchRunner` and code review's ``CodeReviewBatchRunner`` — the ``model`` key of
  the agent step they dispatch;
* ``sizing.models_at_or_above`` — the escalation targets returned for a primary model.

Two controls stop a "resolve everything, always" implementation from passing vacuously: the
escalation ORDER must be unchanged, and with NO classes configured every consumer must produce
today's byte-for-byte value.
"""

from __future__ import annotations

import ast
import dataclasses
from pathlib import Path
from typing import Any

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import sizing
from rebar.llm.workflow.runners import BatchRunRequest, BatchRunResult

# The bare rungs of today's ladder — the strings that must survive byte-for-byte when no class
# is configured, and that must NOT appear in the gate YAML any more.
_TODAY_TRIVIAL = "claude-haiku-4-5"
_TODAY_STANDARD = "claude-sonnet-4-6"
_TODAY_FRONTIER = "claude-opus-4-8"
_TODAY_LADDER = [_TODAY_TRIVIAL, _TODAY_STANDARD, _TODAY_FRONTIER]

_BEDROCK_TRIVIAL = "bedrock:us.anthropic.claude-haiku-4-5"
_BEDROCK_STANDARD = "bedrock:us.anthropic.claude-sonnet-4-6"
_BEDROCK_FRONTIER = "bedrock:us.anthropic.claude-opus-4-8"

_ENV_VARS = tuple(
    f"REBAR_LLM_{cls}_{field}"
    for cls in ("TRIVIAL", "STANDARD", "FRONTIER")
    for field in ("MODEL", "PROVIDER", "ENDPOINT")
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLAN_REVIEW_YAML = _REPO_ROOT / "src" / "rebar" / "llm" / "workflow" / "gates" / "plan-review.yaml"
_CONFIG_PY = _REPO_ROOT / "src" / "rebar" / "llm" / "config.py"


def _set_class_table(monkeypatch, table: dict[str, Any] | None) -> None:
    """Point ``load_class_slots`` at ``table`` and clear the nine env overrides.

    Hermetic on purpose: ``_parse_slot`` applies ``REBAR_LLM_<CLASS>_<FIELD>`` on top of the
    config table, so an ambient override on the developer's machine would silently retarget a
    class and make the no-classes control unfalsifiable.
    """
    from rebar.llm import config as llm_config

    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        llm_config, "_read_llm_file_table", lambda repo_root=None: dict(table or {})
    )


@pytest.fixture
def bedrock_classes(monkeypatch):
    """All three classes retargeted onto Bedrock — the cutover configuration."""
    _set_class_table(
        monkeypatch,
        {
            "model_classes": {
                "trivial": {"model": "us.anthropic.claude-haiku-4-5", "provider": "bedrock"},
                "standard": {"model": "us.anthropic.claude-sonnet-4-6", "provider": "bedrock"},
                "frontier": {"model": "us.anthropic.claude-opus-4-8", "provider": "bedrock"},
            }
        },
    )


@pytest.fixture
def no_classes(monkeypatch):
    """No class configuration at all — today's behaviour must be preserved exactly."""
    _set_class_table(monkeypatch, {})


# ── helpers: a recording agent runner + request builders ──────────────────────────────


class _RecordingAgentRunner:
    """Captures the ``step`` dict of every agent step a batch runner dispatches."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []

    def run(self, ctx):
        self.steps.append(dict(ctx.step))
        from types import SimpleNamespace

        return SimpleNamespace(outputs={"findings": []})

    @property
    def models(self) -> list[Any]:
        return [s.get("model") for s in self.steps]


def _req(ladder: tuple[str, ...], *, step_id: str = "find", **over: Any) -> BatchRunRequest:
    base: dict[str, Any] = {
        "finder": "plan-review-finder",
        "criteria": ({"prompt": "plan-review-A1"},),
        "usd_budget": None,
        "model_ladder": ladder,
        "workflow": {},
        "target_ticket": "abcd-0000-0000-0001",
        "repo_root": None,
        "run_id": "run-1",
        "step_id": step_id,
    }
    base.update(over)
    return BatchRunRequest(**base)


def _production_entry_model(monkeypatch, ladder: tuple[str, ...]) -> str:
    """Run :class:`ProductionBatchRunner` far enough to capture the ``cfg.model`` it hands to
    ``run_pass1`` — the entry rung, which is the whole observable for the Pass-1 split-brain."""
    from rebar.llm.plan_review import production_batch_runner as pbr

    captured: dict[str, str] = {}

    def _fake_run_pass1(ctx, cfg, runner, single, agent, coverage, cap_override=None):
        captured["model"] = cfg.model
        return []

    monkeypatch.setattr(pbr, "run_pass1", _fake_run_pass1)
    monkeypatch.setattr(pbr, "assemble_context", lambda target, repo_root=None: _FakePlanContext())
    monkeypatch.setattr(pbr, "_resolve_criteria", lambda criteria: ([], [], []))
    monkeypatch.setattr(pbr, "_project_criteria", lambda ctx, seen, probe=None: ([], []))

    from rebar.llm.runner import FakeRunner

    result = pbr.ProductionBatchRunner(runner=FakeRunner()).run(_req(ladder))
    assert isinstance(result, BatchRunResult)
    return captured["model"]


class _FakePlanContext:
    """The few PlanContext attributes the runner touches before ``run_pass1``."""

    plan_text = "plan"
    repo_root = None
    centrality = 0.0
    ticket_id = "abcd-0000-0000-0001"
    ticket_type = "task"


# ══ HAPPY PATH ════════════════════════════════════════════════════════════════════════


def test_pass1_entry_rung_resolves_through_the_class_config(monkeypatch, bedrock_classes) -> None:
    """AC: the Pass-1 entry rung resolves through the class config, not a bare model id.

    This is the measured split-brain: `model_ladder[0]` was copied verbatim onto `cfg.model`, so
    a Bedrock-configured run still ran Pass-1 on direct Anthropic.
    """
    assert _production_entry_model(monkeypatch, ("trivial",)) == _BEDROCK_TRIVIAL


def test_config_resolve_model_resolves_a_reserved_class_name(monkeypatch, bedrock_classes) -> None:
    """The workflow-step path: a step whose `model:` is a class name must resolve, not be passed
    to the runner as the literal string `standard`."""
    from rebar.llm import config as llm_config

    cfg = LLMConfig(runner="fake")
    assert llm_config.resolve_model(cfg, step="standard") == _BEDROCK_STANDARD
    assert llm_config.resolve_model(cfg, workflow="frontier") == _BEDROCK_FRONTIER


def test_default_batch_runner_resolves_a_class_named_rung(monkeypatch, bedrock_classes) -> None:
    """AC: `workflow/runners.py` DefaultBatchRunner resolves a class-named rung too — driven with
    `model_ladder: [standard]`, the runner must receive the configured id, not `standard`."""
    from rebar.llm.workflow.runners import DefaultBatchRunner

    agent = _RecordingAgentRunner()
    DefaultBatchRunner().run(_req(("standard",)), agent)
    assert agent.models == [_BEDROCK_STANDARD]


def test_code_review_batch_runner_resolves_a_class_named_rung(monkeypatch, bedrock_classes) -> None:
    """Code review's own batch runner is the third `model_ladder[0]` consumer."""
    from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner

    agent = _RecordingAgentRunner()
    runner = CodeReviewBatchRunner(context="diff")
    runner.run(_req(("standard",), criteria=({"prompt": "code-review-A1"},)), agent)
    assert agent.models == [_BEDROCK_STANDARD]


def test_models_at_or_above_stays_on_bedrock_for_a_bedrock_primary(bedrock_classes) -> None:
    """AC: escalation targets for a BEDROCK primary stay on BEDROCK.

    Fails against the pre-7761 implementation, which returned bare Anthropic ids — so a Bedrock
    run that hit a context limit escalated to direct Anthropic: a hard failure in a Bedrock-only
    container, or silent Anthropic billing wherever a key happened to exist.
    """
    assert sizing.models_at_or_above(_BEDROCK_STANDARD) == [_BEDROCK_STANDARD, _BEDROCK_FRONTIER]
    assert sizing.models_at_or_above(_BEDROCK_TRIVIAL) == [
        _BEDROCK_TRIVIAL,
        _BEDROCK_STANDARD,
        _BEDROCK_FRONTIER,
    ]


def test_plan_review_yaml_ladder_carries_no_literal_model_id() -> None:
    """AC: `grep -n "claude-" plan-review.yaml` returns no model_ladder entry."""
    import yaml

    from rebar.llm.model_classes import CLASS_NAMES

    doc = yaml.safe_load(_PLAN_REVIEW_YAML.read_text())
    ladders = _collect_ladders(doc)
    assert ladders, "no model_ladder found in plan-review.yaml — the AC would pass vacuously"
    for ladder in ladders:
        # This checks THIS test's stated AC — every entry names a model CLASS, so no literal
        # vendor id can pin the gate to one provider. It deliberately does NOT pin the exact
        # list: the entry rung is a separate, ticketed decision (77ed moved it to `frontier`,
        # restoring the opus finder), and only rung 0 is ever read. Asserting the exact triple
        # made an unrelated policy change look like a regression in the class-vocabulary AC.
        assert ladder, ladder
        assert all(e in CLASS_NAMES for e in ladder), ladder
        assert not [e for e in ladder if "claude-" in e]


def _collect_ladders(node: Any) -> list[list[str]]:
    found: list[list[str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "model_ladder" and isinstance(value, list):
                found.append([str(v) for v in value])
            else:
                found.extend(_collect_ladders(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_collect_ladders(item))
    return found


# ══ HELD OUT — back-compat controls and the regression targets ════════════════════════


def test_models_at_or_above_returns_todays_bare_ids_with_no_classes_configured(
    no_classes,
) -> None:
    """THE BACK-COMPAT CONTROL. With no classes configured the return value must be today's BARE
    ids byte-for-byte — not `anthropic:claude-sonnet-4-6`.

    This is what stops a "resolve always" implementation: `resolve_class` runs every name through
    `_resolve_target`, which PREFIXES the inferred provider, so the naive wiring silently changes
    every existing caller's strings. `sizing.models_at_or_above` is asserted against
    literal bare ids at test_plan_review.py:1145, and `prerequisites.py:257` logs these.
    """
    assert sizing.models_at_or_above(_TODAY_TRIVIAL) == _TODAY_LADDER
    assert sizing.models_at_or_above(_TODAY_STANDARD) == [_TODAY_STANDARD, _TODAY_FRONTIER]
    assert sizing.models_at_or_above(_TODAY_FRONTIER) == [_TODAY_FRONTIER]
    assert sizing.models_at_or_above(None) == _TODAY_LADDER
    assert sizing.models_at_or_above("some-unknown-model") == ["some-unknown-model"]


@pytest.mark.parametrize("configured", [False, True])
def test_models_at_or_above_escalation_order_is_unchanged(monkeypatch, configured: bool) -> None:
    """AC: the ladder's escalation ORDER is unchanged — cheapest rung first, frontier last, in
    BOTH configurations. An implementation that resolved rungs through an unordered mapping (a
    dict comprehension keyed by class name, say) can pass "stays on bedrock" and fail here.
    """
    if configured:
        _set_class_table(
            monkeypatch,
            {
                "model_classes": {
                    "trivial": {"model": "us.anthropic.claude-haiku-4-5", "provider": "bedrock"},
                    "standard": {"model": "us.anthropic.claude-sonnet-4-6", "provider": "bedrock"},
                    "frontier": {"model": "us.anthropic.claude-opus-4-8", "provider": "bedrock"},
                }
            },
        )
        expected = [_BEDROCK_TRIVIAL, _BEDROCK_STANDARD, _BEDROCK_FRONTIER]
    else:
        _set_class_table(monkeypatch, {})
        expected = _TODAY_LADDER

    assert sizing.models_at_or_above(None) == expected
    # Every suffix of the ladder is the ladder from that rung upward, in the same order.
    for i, rung in enumerate(expected):
        assert sizing.models_at_or_above(rung) == expected[i:]
    # The sizing TABLE itself is never reordered or rewritten.
    assert [n for n, _w in sizing.MODEL_LADDER] == _TODAY_LADDER


def test_model_ladder_table_stays_bare_names_and_windows() -> None:
    """AC: `sizing.MODEL_LADDER` is UNCHANGED (bare names + windows).

    Putting class names in the table breaks `largest_window_tokens`, whose lookup is a SUBSTRING
    test: `"standard" in "bedrock:us.anthropic.claude-sonnet-4-6"` is False, so every lookup
    would miss and fall through to the ladder maximum — silently changing P8's context budget
    with no error raised.
    """
    assert sizing.MODEL_LADDER == (
        ("claude-haiku-4-5", 200_000),
        ("claude-sonnet-4-6", 1_000_000),
        ("claude-opus-4-8", 1_000_000),
    )


def test_largest_window_tokens_still_matches_a_provider_qualified_id(bedrock_classes) -> None:
    """The substring match must keep working for a provider-qualified id — pinned so it cannot
    silently start missing and returning the ladder maximum for every model."""
    assert sizing.largest_window_tokens(_BEDROCK_STANDARD) == 1_000_000
    assert sizing.largest_window_tokens("bedrock:us.anthropic.claude-haiku-4-5") == 1_000_000
    assert sizing.largest_window_tokens(None) == sizing.MODEL_LADDER[-1][1]
    assert sizing.largest_window_tokens("some-unknown-model") == min(
        w for _, w in sizing.MODEL_LADDER
    )


def test_config_py_does_not_import_model_classes_at_module_level() -> None:
    """AC: the import-cycle guard. `model_classes` imports `config` at module scope and its own
    docstring forbids the reverse, so `config.resolve_model` must import LAZILY inside the
    function body."""
    module = ast.parse(_CONFIG_PY.read_text())
    offenders = [
        ast.unparse(node)
        for node in module.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        and "model_classes"
        in ((getattr(node, "module", "") or "") + "".join(a.name for a in node.names))
    ]
    assert offenders == []


def test_a_ladder_of_literal_ids_still_resolves_to_those_ids_unchanged(
    monkeypatch, bedrock_classes
) -> None:
    """AC: a ladder of literal ids still resolves to those ids unchanged — even with classes
    configured. A literal id is NOT a class name, so it must round-trip through every consumer.
    """
    from rebar.llm.workflow.runners import DefaultBatchRunner

    assert _production_entry_model(monkeypatch, (_TODAY_FRONTIER,)) == _TODAY_FRONTIER

    agent = _RecordingAgentRunner()
    DefaultBatchRunner().run(_req((_BEDROCK_STANDARD,)), agent)
    assert agent.models == [_BEDROCK_STANDARD]

    agent2 = _RecordingAgentRunner()
    DefaultBatchRunner().run(_req(("openai:gpt-4o",)), agent2)
    assert agent2.models == ["openai:gpt-4o"]


def test_an_empty_ladder_still_leaves_the_model_unset(bedrock_classes) -> None:
    """The `None` branch of `model_ladder[0] if req.model_ladder else None` must survive: an
    empty ladder means "no per-step model", which is NOT the same as resolving a class."""
    from rebar.llm.workflow.runners import DefaultBatchRunner

    agent = _RecordingAgentRunner()
    DefaultBatchRunner().run(_req(()), agent)
    assert agent.models == [None]
    assert all("model" not in s for s in agent.steps)


def test_resolve_model_precedence_is_undisturbed(bedrock_classes) -> None:
    """Class resolution applies to the WINNER of the step > workflow > config precedence, never
    reorders it. `tests/interfaces/store/test_scoped_ticket_tool.py::test_resolve_model_precedence`
    pins the same order with literal ids."""
    from rebar.llm import config as llm_config

    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="trivial")
    assert llm_config.resolve_model(cfg, step="standard", workflow="frontier") == _BEDROCK_STANDARD
    assert llm_config.resolve_model(cfg, workflow="frontier") == _BEDROCK_FRONTIER
    assert llm_config.resolve_model(cfg) == _BEDROCK_TRIVIAL


def test_resolve_model_leaves_a_literal_step_model_untouched(no_classes) -> None:
    """With no classes configured, `resolve_model` must be byte-for-byte what it was: a passthrough
    of the precedence winner."""
    from rebar.llm import config as llm_config

    cfg = dataclasses.replace(LLMConfig(runner="fake"), model="claude-opus-4-8")
    assert llm_config.resolve_model(cfg, step="anthropic:claude-opus-4-8") == (
        "anthropic:claude-opus-4-8"
    )
    assert llm_config.resolve_model(cfg, workflow="openai:gpt-4o") == "openai:gpt-4o"
    assert llm_config.resolve_model(cfg) == "claude-opus-4-8"


# ══ Escalation targets must stay on the provider the RUN is on (bug 7de4) ═════════════
#
# The rungs above are about the class VOCABULARY. These are about the run's own model: with
# no class table configured, `_rung_target` compared the rung against itself, so the model the
# run is actually on was never an input and every escalation target degraded to MODEL_LADDER's
# bare Anthropic name — relocating a Bedrock run onto direct Anthropic.

_BEDROCK_PINNED_FRONTIER = "bedrock:us.anthropic.claude-opus-4-8-v1:0"
_BEDROCK_PINNED_TRIVIAL = "bedrock:us.anthropic.claude-haiku-4-5-v1:0"


def _recording_pass1_chunk(monkeypatch, *, fail_all: bool = False) -> list[tuple[str, tuple]]:
    """Replace ``passes.pass1_chunk`` with a stub that raises a context-limit error on the
    batch call (and, when ``fail_all``, on every single-criterion call too), recording the
    ``cfg.model`` each attempt was dispatched with."""
    observed: list[tuple[str, tuple]] = []

    def _stub(runner, cfg, *, plan, chunk, agentic=False, extra_context=""):
        observed.append((cfg.model, tuple(c["id"] for c in chunk)))
        if fail_all or len(chunk) > 1:
            raise RuntimeError("prompt is too long")
        return [{"finding": "f", "criteria": [chunk[0]["id"]]}], {}

    monkeypatch.setattr(sizing.passes, "pass1_chunk", _stub)
    return observed


def _run_ladder(cfg_model: str, *, events: list[str] | None = None):
    cfg = dataclasses.replace(LLMConfig(runner="fake"), model=cfg_model)
    return sizing.pass1_with_ladder(
        object(),
        cfg,
        "plan",
        [{"id": "A1"}, {"id": "A2"}],
        False,
        events if events is not None else [],
    )


def test_pass1_ladder_retry_stays_on_the_run_s_own_bedrock_model(monkeypatch, no_classes) -> None:
    """AC: with no class table and a Bedrock-qualified primary, the model that reaches
    ``passes.pass1_chunk`` on the one-criterion retry is the primary itself.

    Observed on the ESCALATION PATH, not on ``models_at_or_above`` in isolation: the entry rung
    is already class-resolved, so an entry-rung test structurally cannot catch this.
    """
    observed = _recording_pass1_chunk(monkeypatch)

    findings, _calls = _run_ladder(_BEDROCK_PINNED_FRONTIER)

    retries = [model for model, ids in observed if len(ids) == 1]
    assert retries == [_BEDROCK_PINNED_FRONTIER, _BEDROCK_PINNED_FRONTIER]
    assert len(findings) == 2


def test_models_at_or_above_keeps_a_bedrock_primary_on_bedrock_with_no_classes(
    no_classes,
) -> None:
    """AC (worked example): the primary's own rung is returned verbatim rather than degraded to
    the bare Anthropic family name."""
    assert sizing.models_at_or_above(_BEDROCK_PINNED_FRONTIER) == [_BEDROCK_PINNED_FRONTIER]


def test_models_at_or_above_drops_unnameable_higher_rungs(no_classes) -> None:
    """AC: a higher rung that cannot be named in the run's provider is DROPPED, never crossed.

    The class vocabulary is the only thing that could name sonnet/opus on Bedrock and it is not
    configured, so there is no honest target — escalation stops instead of jumping provider.
    """
    targets = sizing.models_at_or_above(_BEDROCK_PINNED_TRIVIAL)
    assert targets == [_BEDROCK_PINNED_TRIVIAL]
    assert not [t for t in targets if "claude-sonnet" in t or "claude-opus" in t]


def test_dropped_rungs_still_produce_the_too_big_failure_finding(monkeypatch, no_classes) -> None:
    """AC: dropping unnameable rungs must not turn escalation into a crash or a silent empty
    result — the existing ``_too_big`` finding still reports the size failure."""
    _recording_pass1_chunk(monkeypatch, fail_all=True)

    findings, _calls = _run_ladder(_BEDROCK_PINNED_TRIVIAL)

    assert [f["criteria"] for f in findings] == [["A1"], ["A2"]]
    assert all(f["_too_big"] is True for f in findings)


def test_start_rung_location_for_a_foreign_family_is_explicit(no_classes) -> None:
    """A primary from a non-Anthropic model FAMILY matches no ladder rung. That is an explicit
    outcome, not a silent substitution: the ladder is the primary alone, so a retry re-runs on
    the operator's own model instead of DOWNGRADING onto a smaller Anthropic rung, and the window
    is the ladder MINIMUM so the P8 size gate cannot under-block on an overstated budget."""
    assert sizing.models_at_or_above("bedrock:us.amazon.nova-pro-v1:0") == [
        "bedrock:us.amazon.nova-pro-v1:0"
    ]
    assert sizing.largest_window_tokens("bedrock:us.amazon.nova-pro-v1:0") == min(
        w for _, w in sizing.MODEL_LADDER
    )
