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
