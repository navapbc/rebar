"""Unit tests for the Cupid ticket-digest enrichment op (epic only-crave-art, ee3d).

All tests are offline and deterministic — they use ``FakeRunner(structured=...)`` (no live
LLM). ``test_enrich_quality_live`` is the only path that would call a real model and is
skipped unless ``REBAR_RUN_LLM_EVAL=1``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import rebar.schemas as schemas
from rebar.llm.config import LLMConfig
from rebar.llm.contracts import response_model_for
from rebar.llm.enrich import enrich
from rebar.llm.errors import LLMUnavailableError, StructuredOutputError
from rebar.llm.findings import FindingsError, finalize_outcome
from rebar.llm.prompting import prompts
from rebar.llm.runner import FakeRunner, Runner, RunRequest

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "enrich_quality"

_VALID_DIGEST = {
    "problem_keywords": ["login", "authentication", "session"],
    "component_or_area": "auth subsystem",
    "key_entities": ["SessionToken", "login_handler"],
    "propositions": ["users cannot authenticate", "session token is not persisted"],
}


def _cfg() -> LLMConfig:
    # repo_path=None → the packaged prompt is used (no project override needed).
    return LLMConfig()


def test_schema_valid() -> None:
    out = enrich(
        text="Login is broken; users cannot log in.",
        config=_cfg(),
        runner=FakeRunner(structured=dict(_VALID_DIGEST)),
    )
    assert set(out) == {"digest", "low_proposition_count"}
    # The digest validates against the ticket_digest schema.
    schemas.validator("ticket_digest").validate(out["digest"])
    assert out["low_proposition_count"] is False


def test_no_nondeterministic_fields() -> None:
    out = enrich(text="anything", config=_cfg(), runner=FakeRunner(structured=dict(_VALID_DIGEST)))
    # Exactly the four schema fields — no runner/model/trace_id provenance, no timestamps.
    assert set(out["digest"]) == {
        "problem_keywords",
        "component_or_area",
        "key_entities",
        "propositions",
    }


def test_propositions_bounded() -> None:
    cfg = _cfg()  # min=2, max=6
    # Above max → truncated to max.
    big = dict(_VALID_DIGEST, propositions=[f"p{i}" for i in range(9)])
    out = enrich(text="x", config=cfg, runner=FakeRunner(structured=big))
    assert len(out["digest"]["propositions"]) == cfg.overlap_propositions_max
    assert out["low_proposition_count"] is False
    # Below min → kept, flagged, never raises.
    small = dict(_VALID_DIGEST, propositions=["only one"])
    out2 = enrich(text="x", config=cfg, runner=FakeRunner(structured=small))
    assert out2["digest"]["propositions"] == ["only one"]
    assert out2["low_proposition_count"] is True


def test_bad_shape() -> None:
    # Missing `propositions` → the runner's validate_structured raises FindingsError before
    # the op sees a result; nothing is returned/written.
    bad = {"problem_keywords": ["a"], "component_or_area": "b", "key_entities": ["c"]}
    with pytest.raises(FindingsError):
        enrich(text="x", config=_cfg(), runner=FakeRunner(structured=bad))


class _UnavailableRunner(Runner):
    """Stub standing in for the pydantic_ai runner when the ``agents`` extra / API key is
    absent — its preflight/run raise LLMUnavailableError, exactly as the real runner does."""

    name = "unavailable"

    def preflight(self) -> None:
        raise LLMUnavailableError("the 'agents' extra / API key is absent")

    def run(self, req: RunRequest) -> dict:
        raise LLMUnavailableError("the 'agents' extra / API key is absent")


def test_absent_llm() -> None:
    with pytest.raises(LLMUnavailableError):
        enrich(text="x", config=_cfg(), runner=_UnavailableRunner())


def test_enrich_exported() -> None:
    # The op is exported from the package facade …
    from rebar.llm import enrich as exported_enrich

    assert callable(exported_enrich)
    # … and the ticket_digest contract is registered (not the default findings model).
    model = response_model_for("ticket_digest")
    assert model.__name__ == "TicketDigest"
    assert model is not response_model_for("some_unregistered_schema")


def test_prompt_excludes_logs() -> None:
    prompt = prompts.get_prompt("ticket-digest")
    text = prompt.text.lower()
    assert "log" in text and ("stack trace" in text or "stack-trace" in text or "traceback" in text)
    assert "discard" in text or "do not copy" in text or "never copy" in text


def test_prompt_frontmatter() -> None:
    prompt = prompts.get_prompt("ticket-digest")
    assert prompt.execution_mode == "single_turn"
    assert prompt.outputs == "ticket_digest"
    # Not a reviewer — must never be selected as a plan/ticket reviewer.
    assert prompt.category != "review"


def test_finalize_absent_structured() -> None:
    # The live-path guarantee a FakeRunner cannot reproduce: an absent structured response
    # is a hard StructuredOutputError, never a clean empty digest.
    with pytest.raises(StructuredOutputError):
        finalize_outcome(
            {"structured_response": None},
            mode="structured",
            output_schema="ticket_digest",
            runner="fake",
        )


def _load_fixtures() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(_FIXTURE_DIR.glob("*.json"))]


def test_enrich_quality() -> None:
    fixtures = _load_fixtures()
    assert len(fixtures) >= 6, f"expected >=6 quality fixtures, got {len(fixtures)}"
    cfg = _cfg()
    for fx in fixtures:
        # Exercise the real op path via the text= injection seam, with the fixture's
        # captured digest as the canned model output.
        out = enrich(text=fx["body"], config=cfg, runner=FakeRunner(structured=dict(fx["digest"])))
        haystack = {
            s.lower() for s in out["digest"]["key_entities"] + out["digest"]["problem_keywords"]
        }
        gold = [g.lower() for g in fx["gold"]]
        assert any(g in haystack for g in gold), (
            f"{fx['ticket_id']}: no gold entity/keyword {fx['gold']} in "
            f"key_entities ∪ problem_keywords {sorted(haystack)}"
        )


@pytest.mark.skipif(
    os.environ.get("REBAR_RUN_LLM_EVAL") != "1",
    reason="live LLM eval; set REBAR_RUN_LLM_EVAL=1 to regenerate fixture digests",
)
def test_enrich_quality_live() -> None:  # pragma: no cover - not run in CI
    fixtures = _load_fixtures()
    assert len(fixtures) >= 6
    for fx in fixtures:
        out = enrich(text=fx["body"])  # real runner
        haystack = {
            s.lower() for s in out["digest"]["key_entities"] + out["digest"]["problem_keywords"]
        }
        gold = [g.lower() for g in fx["gold"]]
        assert any(g in haystack for g in gold)


# --- Prompt bounding against the resolved model's window (bug spongy-illjudged-terrier) ------


class _CapturingRunner(Runner):
    """Records the RunRequest it was handed, then returns a valid digest."""

    name = "capture"

    def __init__(self) -> None:
        self.seen: RunRequest | None = None

    def run(self, req: RunRequest) -> dict:
        self.seen = req
        return {**_VALID_DIGEST, "runner": self.name, "model": None, "trace_id": None}

    def preflight(self) -> None:
        pass


# The conservative physical ceiling this op must respect: the resolved model's OWN context
# window at 2 chars/token (English prose averages ~4, so 2 under-admits deliberately). This
# mirrors `workflow.completion_criteria._CONTEXT_CHARS_PER_TOKEN`, the existing precedent for
# deriving a character ceiling from a model window.
def _ceiling_for(model: str) -> int:
    from rebar.llm.model_classes import own_window_tokens

    return own_window_tokens(model) * 2


def test_oversized_source_is_bounded_before_the_wire() -> None:
    """An over-window source never reaches the runner at full length.

    The observable contract: whatever `enrich()` hands the runner as `instructions` fits the
    resolved model's own context window, and the shortening is VISIBLE (a marker), never a
    silent drop. Asserted on the request object handed to the injected runner seam — an
    observable boundary, not an internal name.
    """
    cfg = LLMConfig(model="bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0")
    ceiling = _ceiling_for("claude-haiku-4-5")
    source = "x" * (ceiling * 3)
    runner = _CapturingRunner()
    enrich(text=source, config=cfg, runner=runner)
    assert runner.seen is not None
    sent = runner.seen.instructions or ""
    assert len(sent) <= ceiling, f"sent {len(sent)} chars against a {ceiling}-char ceiling"
    assert "truncated" in sent.lower(), "an over-long source must be shortened VISIBLY"


def test_source_within_the_ceiling_is_passed_through_untouched() -> None:
    """The bound is inert below the ceiling — a normal ticket's prompt is byte-identical."""
    cfg = LLMConfig(model="bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0")
    source = "Login is broken; users cannot log in."
    runner = _CapturingRunner()
    enrich(text=source, config=cfg, runner=runner)
    assert runner.seen is not None
    assert runner.seen.instructions == source


def test_bounding_an_already_bounded_source_is_a_no_op() -> None:
    """Applying the bound twice yields the same prompt as applying it once.

    Idempotence matters because a digest is keyed by content hash: a non-idempotent bound
    would make the same ticket produce different prompts on successive drains.
    """
    cfg = LLMConfig(model="bedrock:us.anthropic.claude-haiku-4-5-20251001-v1:0")
    first = _CapturingRunner()
    enrich(text="z" * (_ceiling_for("claude-haiku-4-5") * 4), config=cfg, runner=first)
    assert first.seen is not None
    once = first.seen.instructions or ""
    second = _CapturingRunner()
    enrich(text=once, config=cfg, runner=second)
    assert second.seen is not None
    assert second.seen.instructions == once
