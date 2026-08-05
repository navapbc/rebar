"""Live structured-output measurement harness (story a40f) — the model call only.

WHY THIS EXISTS. This is the shared, operator-triggered measurement instrument for the df3a
schema-filtered selection parser and its siblings (the sentinel output-format directive and the
capability-rows matrix). It sweeps a matrix of provider CELLS × directive VARIANTS × production-
shaped PROMPTS, makes the live model calls, captures every raw reply as a CI artifact, and scores
the replies into a before/after parse-success table.

Everything DETERMINISTIC — the reply scorer, the layout classifier, the per-cell credential gate,
and the call-budget cap — lives in ``_structured_matrix`` (imported as ``sm``) and is proven
offline on committed golden fixtures by ``tests/unit/test_structured_matrix_scorer.py``. ONLY the
model call is live here, so the harness itself carries almost no untested logic: the paid run
exercises the wire, not the scoring.

Gated exactly like every other live-LLM module in this tier (``_live_llm``, story f124): marked
``external`` (inert in the default suite), auto-marked ``llm_live`` via the module-level
``_live_llm_ready`` sentinel (tests/external/conftest.py), and skipped by
``_live_llm.skip_without_live_llm`` when no credential is present for the configured provider.

Run::  REBAR_RUN_EXTERNAL=1 ANTHROPIC_API_KEY=… \
           pytest -m "external and llm_live" tests/external/test_structured_output_matrix.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import _live_llm
import _structured_matrix as sm
import pytest

import rebar  # noqa: F401 — ensures the package (and its conftest fixtures) import cleanly
from rebar.llm import structured
from rebar.llm.contracts import completion_verdict_response_model

pytestmark = pytest.mark.external

# Auto-marks this module's tests `llm_live` (tests/external/conftest.py) — the dimension the
# provider matrix selects on and the all-skip canary counts. REQUIRED: a module that uses the
# shared live-LLM gate without this sentinel fails tests/unit/test_ci_provider_matrix.py.
_live_llm_ready = _live_llm.live_llm_ready()

# The committed provider overlays define the matrix cells (one arm per `*.toml`).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROVIDERS_DIR = _REPO_ROOT / ".github" / "llm-providers"

# ── the two production-shaped prompts ───────────────────────────────────────────────────────
#
# (a) A df3a-shaped agentic transcript whose tool output QUOTES a dependency-link record before
#     the real CompletionVerdict step — the exact multi-object shape whose first-object parse
#     drops the verdict (bug df3a). (b) A single-shot contract step.

_AGENTIC_TRANSCRIPT_PROMPT = (
    "You are a completion verifier finishing an agentic review turn.\n\n"
    "A dependency-lookup tool returned this record verbatim:\n"
    '  {"relation": "depends_on", "target_id": "f2f3-9cb1-335b-4e31"}\n\n'
    "Having read it, emit your FINAL CompletionVerdict for the ticket: a JSON object with "
    "`verdict` (PASS or FAIL), a `findings` array, and a `summary` string. The acceptance "
    "criteria are met, so return PASS."
)

_SINGLE_SHOT_PROMPT = (
    "Return a CompletionVerdict for a ticket whose single acceptance criterion is demonstrably "
    "met: a JSON object with `verdict` (PASS or FAIL), a `findings` array, and a `summary` "
    "string. Return PASS."
)

#: ``(label, prompt)`` pairs — the two production-shaped prompts each cell/variant is measured on.
PROMPTS: tuple[tuple[str, str], ...] = (
    ("agentic_transcript", _AGENTIC_TRANSCRIPT_PROMPT),
    ("single_shot", _SINGLE_SHOT_PROMPT),
)

#: The output-format directive the ``sentinel`` variant appends to the base prompt. Sourced
#: from production so the live sweep exercises the real marker directive; the leading "\n\n"
#: preserves spacing from the prompt.
_SENTINEL_DIRECTIVE = "\n\n" + structured.SENTINEL_DIRECTIVE

#: How many times each (cell × variant × prompt) is repeated, to sample non-determinism.
_N_REPEATS = 10


def _apply_variant(prompt: str, variant: str) -> str:
    """The prompt as sent under a directive ``variant``: ``current`` is the base prompt;
    ``sentinel`` appends the output-format directive."""
    if variant == "sentinel":
        return prompt + _SENTINEL_DIRECTIVE
    return prompt


def _max_calls_cap() -> int:
    """The per-run live-call budget, from ``REBAR_STRUCTURED_MATRIX_MAX_CALLS`` (default
    :data:`sm.DEFAULT_CALL_BUDGET`)."""
    raw = os.environ.get("REBAR_STRUCTURED_MATRIX_MAX_CALLS", "").strip()
    if not raw:
        return sm.DEFAULT_CALL_BUDGET
    try:
        return int(raw)
    except ValueError:
        return sm.DEFAULT_CALL_BUDGET


def _artifact_dir(tmp_path: Path) -> Path:
    """The directory raw replies + the score table are written to
    (``REBAR_STRUCTURED_MATRIX_ARTIFACT_DIR``, default a pytest tmp path)."""
    configured = os.environ.get("REBAR_STRUCTURED_MATRIX_ARTIFACT_DIR", "").strip()
    out = Path(configured) if configured else (tmp_path / "structured-matrix-artifacts")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _live_reply(prompt: str, config) -> str:
    """One live model call returning the model's RAW reply text (no structured coercion).

    Uses the same ``PydanticAIRunner`` text-mode / single-turn path the other ``*_live`` modules
    exercise (see tests/external/test_pydantic_ai_cutover_live.py::test_pydantic_text_mode): a
    single model call with NO tools, so we capture exactly what the model emits before any
    parsing."""
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    runner = PydanticAIRunner(config)
    runner.preflight()
    req = RunRequest(
        system_prompt="You are a completion verifier that emits a single structured verdict.",
        instructions=prompt,
        config=config,
        mode="text",
        reviewers=[],
        execution_mode="single_turn",
    )
    out = runner.run(req)
    return out.get("text") or out.get("summary") or out.get("output") or ""


@_live_llm.skip_without_live_llm
def test_structured_output_matrix(rebar_repo: Path, tmp_path: Path) -> None:
    """Sweep the credentialed cells, capture raw replies, and score them before/after.

    The scoring is the deterministic ``sm`` logic proven offline; here the value added is the LIVE
    reply. Cells whose provider credential is absent are recorded as ``unmeasured`` and skipped
    WITHOUT failing (a partial matrix is a legitimate operator run); the shared all-skip canary
    still guards a run in which EVERY live test skipped."""
    from rebar.llm.config import LLMConfig

    cells = sm.load_cells(_PROVIDERS_DIR)

    # Refuse an over-budget sweep BEFORE constructing any network object — a pure integer check.
    cap = _max_calls_cap()
    sm.enforce_call_budget(
        sm.planned_call_count(len(cells), len(sm.DIRECTIVE_VARIANTS), len(PROMPTS), _N_REPEATS),
        cap=cap,
    )

    model_cls = completion_verdict_response_model()
    artifact_dir = _artifact_dir(tmp_path)

    measured_any = False
    all_scores: dict[str, dict] = {}
    unmeasured: list[str] = []

    for cell in cells:
        if sm.credential_status(cell.provider) == "unmeasured":
            unmeasured.append(cell.provider)
            continue
        measured_any = True

        # This cell's provider is selected through its committed overlay, exactly as the CI
        # matrix does — so the harness dogfoods the same REBAR_LLM_CONFIG_FILE override path.
        config = LLMConfig.from_env(repo_root=str(rebar_repo))

        for variant in sm.DIRECTIVE_VARIANTS:
            replies: list[str] = []
            for prompt_label, prompt in PROMPTS:
                sent = _apply_variant(prompt, variant)
                for repeat in range(_N_REPEATS):
                    reply = _live_reply(sent, config)
                    replies.append(reply)
                    name = f"{cell.provider}__{variant}__{prompt_label}__{repeat:02d}.txt"
                    (artifact_dir / name).write_text(reply, encoding="utf-8")

            table = sm.score_replies(replies, model_cls)
            all_scores[f"{cell.provider}__{variant}"] = {
                "provider": cell.provider,
                "variant": variant,
                "n": table.n,
                "current_ok": table.current_ok,
                "new_ok": table.new_ok,
                "layout_counts": table.layout_counts,
            }

    # The before/after score table — the run's headline artifact.
    (artifact_dir / "score_table.json").write_text(
        json.dumps(
            {"scores": all_scores, "unmeasured": unmeasured, "call_budget": cap},
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    if not measured_any:
        pytest.skip(
            f"no credentialed cells to measure (unmeasured: {unmeasured or 'none'}) — "
            "the all-skip canary guards a wholly-skipped session"
        )
    assert all_scores, "at least one measured cell must produce a score table"
