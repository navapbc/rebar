"""The verifier paths resolve the `standard` class, not an equality heuristic (task 172e).

MEASURED defect this replaces: `resolve_verifier_model` downgraded ONLY when `cfg.model` was
EXACTLY the bare `"claude-opus-4-8"`. Provider-qualifying the SAME model
(`anthropic:claude-opus-4-8`) — or using any Bedrock id — read as "the operator chose this", so
Pass-2/Pass-4 silently inherited the frontier model, losing both the cost downgrade and (on a model
that rejects sampling params) greedy decoding.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_STANDARD_TODAY = "claude-sonnet-4-6"


def _no_classes(monkeypatch):
    from rebar.llm import config as llm_config

    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: {})


@pytest.mark.parametrize(
    "cfg_model",
    [
        "claude-opus-4-8",
        "anthropic:claude-opus-4-8",
        "bedrock:us.anthropic.claude-opus-4-8",
        "bedrock:us.anthropic.claude-sonnet-4-6",
        "openai:gpt-4o",
    ],
)
def test_the_verifier_resolves_standard_whatever_cfg_model_is(monkeypatch, cfg_model) -> None:
    """The core fix. EVERY one of these must land on the standard class. The middle rows are the
    ones that fail today: `anthropic:claude-opus-4-8` is the SAME model as the bare default, merely
    provider-qualified, and it silently kept Pass-2 on opus."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import _verifier_cfg

    _no_classes(monkeypatch)
    out = _verifier_cfg(LLMConfig(repo_path=".", model=cfg_model))
    assert out.model.endswith(_STANDARD_TODAY), (
        f"cfg.model={cfg_model} left the verifier on {out.model}, not the standard class"
    )


def test_the_verifier_follows_a_configured_standard_class(monkeypatch) -> None:
    """The class config is what an operator now steers the verifier with — so a Bedrock standard
    class must move Pass-2 to Bedrock, which is what makes the cutover keep its two-tier shape."""
    from rebar.llm import config as llm_config
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import _verifier_cfg

    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {
            "model_classes": {
                "standard": {"model": "us.anthropic.claude-sonnet-4-6", "provider": "bedrock"}
            }
        },
    )
    out = _verifier_cfg(LLMConfig(repo_path=".", model="bedrock:us.anthropic.claude-opus-4-8"))
    assert out.model == "bedrock:us.anthropic.claude-sonnet-4-6"


def test_completion_verifier_also_resolves_standard(monkeypatch) -> None:
    """completion.py carried a SEPARATE copy of the same equality test (line ~332), so fixing only
    the plan-review path would leave the completion gate on the frontier model."""
    from rebar.llm import completion

    _no_classes(monkeypatch)
    resolved = completion._verifier_model_for_completion()
    assert resolved.endswith(_STANDARD_TODAY)


def test_nothing_configured_is_byte_identical_to_todays_verifier(monkeypatch) -> None:
    """The rollback guarantee: with no class config the verifier must still be exactly today's
    VERIFIER_DEFAULT_MODEL, so this change is invisible to every existing deployment."""
    from rebar.llm.config import VERIFIER_DEFAULT_MODEL, LLMConfig
    from rebar.llm.plan_review import _verifier_cfg

    _no_classes(monkeypatch)
    out = _verifier_cfg(LLMConfig(repo_path=".", model="claude-opus-4-8"))
    assert out.model.endswith(VERIFIER_DEFAULT_MODEL)


# ── through the REAL callers, not in isolation ───────────────────────────────────────────


class _RecordingRunner:
    """A runner that records the ``config`` each RunRequest carries and returns a canned
    structured payload. The point is the CONFIG, not the answer: both xcheck sub-calls build
    ``RunRequest(config=_verifier_cfg(cfg))``, so what this captures IS the model the verifier
    path actually runs under."""

    name = "fake"

    def __init__(self, structured: dict):
        self._structured = structured
        self.seen_models: list[str] = []

    def preflight(self) -> None:
        """Always ready — no extra, no network."""

    def run(self, req) -> dict:
        self.seen_models.append(req.config.model)
        return dict(self._structured)


def _xcheck_active(monkeypatch, *, contradiction=False, comment_trail=False):
    """Both entries are config-gated and inert by default, so a test that forgets this passes
    vacuously — the sub-call never runs and no model is ever resolved."""
    import types

    from rebar import config as core_config

    monkeypatch.setattr(
        core_config,
        "load_config",
        lambda repo_root=None: types.SimpleNamespace(
            verify=types.SimpleNamespace(
                contradiction_xcheck_active=contradiction,
                comment_trail_xcheck_active=comment_trail,
            )
        ),
    )


def _two_finding_verdict() -> dict:
    """Two surfaced findings — the minimum both sub-calls require before they will run."""
    return {
        "verdict": "BLOCK",
        "blocking": [{"id": "b0", "priority": 0.85, "criteria": ["E2"], "finding": "a claim"}],
        "advisory": [{"id": "a0", "priority": 0.4, "criteria": ["F1"], "finding": "its refuter"}],
        "dropped": [],
        "coverage": {"counts": {"blocking": 1, "advisory_surfaced": 1, "dropped": 0}},
    }


def test_contradiction_xcheck_resolves_standard_through_its_real_caller(monkeypatch) -> None:
    """`_verifier_cfg` resolving correctly in isolation does not prove the CALLERS use it — a
    caller that passed plain `cfg` would leave this sub-call on the frontier model while every
    isolated test stayed green. Asserted at xcheck.py's contradiction site."""
    import types

    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import xcheck

    _no_classes(monkeypatch)
    _xcheck_active(monkeypatch, contradiction=True)
    rr = _RecordingRunner({"pairs": []})
    xcheck.maybe_apply_contradiction(
        "t",
        _two_finding_verdict(),
        ctx=types.SimpleNamespace(plan_text="p", state={}),
        cfg=LLMConfig(repo_path=".", runner="fake", model="anthropic:claude-opus-4-8"),
        runner=rr,
        repo_root=None,
    )
    assert rr.seen_models, "the contradiction sub-call never ran — the gate or the fixture is off"
    assert all(m.endswith(_STANDARD_TODAY) for m in rr.seen_models), (
        f"contradiction xcheck ran on {rr.seen_models}, not the standard class"
    )
    assert not any("opus" in m for m in rr.seen_models), "cfg.model leaked into the verifier call"


def test_comment_trail_xcheck_resolves_standard_through_its_real_caller(monkeypatch) -> None:
    """The second call site. It reads its trail from ctx.state['comments'], and returns early
    with no trail — so an empty trail here would make this pass without resolving anything."""
    import types

    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import xcheck

    _no_classes(monkeypatch)
    _xcheck_active(monkeypatch, comment_trail=True)
    rr = _RecordingRunner({"assessments": []})
    xcheck.maybe_apply_comment_trail(
        "t",
        _two_finding_verdict(),
        ctx=types.SimpleNamespace(
            plan_text="p", state={"comments": [{"author": "x", "body": "a prior decision"}]}
        ),
        cfg=LLMConfig(repo_path=".", runner="fake", model="bedrock:us.anthropic.claude-opus-4-8"),
        runner=rr,
        repo_root=None,
    )
    assert rr.seen_models, "the comment-trail sub-call never ran — gate or trail fixture is off"
    assert all(m.endswith(_STANDARD_TODAY) for m in rr.seen_models), (
        f"comment-trail xcheck ran on {rr.seen_models}, not the standard class"
    )
    assert not any("opus" in m for m in rr.seen_models), "cfg.model leaked into the verifier call"
