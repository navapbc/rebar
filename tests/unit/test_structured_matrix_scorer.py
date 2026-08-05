"""Offline unit oracle for the structured-output measurement harness (story a40f).

The live measurement itself is operator-triggered and its evidence is an uploaded CI artifact
(the epic's shared harness for the sentinel + capability-rows siblings). Everything DETERMINISTIC
about that harness — the reply scorer, the per-cell credential gate, the call-budget cap, and the
committed workflow's discipline — is proven here, offline, on committed golden fixtures, so the
scoring logic is never validated for the first time by a paid live run.

The scorer's pure logic lives in ``tests/external/_structured_matrix.py`` (an underscore helper,
imported by both the live harness ``tests/external/test_structured_output_matrix.py`` and this
unit test, exactly as ``tests/external/_live_llm.py`` is shared today). This file inserts that
directory on ``sys.path`` so the unit tier can import it without collecting the external module.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_EXTERNAL = _ROOT / "tests" / "external"
if str(_EXTERNAL) not in sys.path:
    sys.path.insert(0, str(_EXTERNAL))

import _structured_matrix as sm  # noqa: E402  (path insertion must precede the import)

from rebar.llm.contracts import completion_verdict_response_model  # noqa: E402

_CORPUS = _ROOT / "tests" / "fixtures" / "structured_reply_corpus"


def _model():
    return completion_verdict_response_model()


def _reply(name: str) -> str:
    import json

    return json.loads((_CORPUS / f"{name}.json").read_text())["reply"]


# The four parse-success goldens (v1..v4) and their MEASURED current/new parse outcomes. Pinned
# from a live probe against the committed parser: the old first-object path recovers the verdict
# only when it is rendered first (v4); the new schema-filtered selection recovers it in all four.
_PARSE_GOLDENS = {
    "v1_bare_dep_before_verdict": (False, True),
    "v2_fenced_dep_before_verdict": (False, True),
    "v3_both_fenced_verdict_last": (False, True),
    "v4_verdict_before_dep_passes_today": (True, True),
}

# The four layout goldens, one clean example per class.
_LAYOUT_GOLDENS = {
    "layout_prose": "prose",
    "layout_fenced": "fenced",
    "layout_multi_object": "multi_object",
    "layout_quoted_json": "quoted_json",
}


# --------------------------------------------------------------------------------------------
# HAPPY PATH — the scorer reproduces the committed goldens.
# --------------------------------------------------------------------------------------------


def test_scorer_reproduces_parse_success_golden_counts() -> None:
    """The before/after table on the df3a corpus is EXACTLY current 1/4, new 4/4.

    This is the whole point of the measurement: the selection fix turns three of four
    production-shaped replies from a hard parse failure into a success while never regressing
    the one that already parsed. The scorer must reproduce that table byte-for-byte from the
    committed replies, per-fixture and in aggregate.
    """
    model = _model()
    replies = []
    for name, (want_current, want_new) in _PARSE_GOLDENS.items():
        text = _reply(name)
        score = sm.score_reply(text, model)
        assert score.current_ok is want_current, name
        assert score.new_ok is want_new, name
        replies.append(text)

    table = sm.score_replies(replies, model)
    assert table.n == 4
    assert table.current_ok == 1
    assert table.new_ok == 4


def test_scorer_reproduces_layout_class_golden_distribution() -> None:
    """Each layout golden classifies to its one declared class, and the aggregate is 1 each.

    The sentinel sibling reasons over these rates (quote-in-prose 0%->100%), so the four-way
    classifier must be pinned: a golden per class, and a distribution with every class present.
    """
    model = _model()
    replies = []
    for name, want_layout in _LAYOUT_GOLDENS.items():
        text = _reply(name)
        assert sm.classify_layout(text) == want_layout, name
        assert sm.score_reply(text, model).layout == want_layout, name
        replies.append(text)

    table = sm.score_replies(replies, model)
    assert set(table.layout_counts) == set(sm.LAYOUT_CLASSES)
    assert table.layout_counts == {
        "prose": 1,
        "fenced": 1,
        "multi_object": 1,
        "quoted_json": 1,
    }


# --------------------------------------------------------------------------------------------
# HELD OUT — credential gate, call budget, workflow discipline.
# --------------------------------------------------------------------------------------------


def test_key_authenticated_cell_is_measured_only_when_its_key_is_present() -> None:
    """A cell is 'measured' iff THIS provider's own credential is present — never a foreign key.

    The skip path is python logic (not YAML), so it is unit-testable: an absent key must record
    'unmeasured' and skip, so a scheduled run with a missing secret cannot go green having
    measured nothing.
    """
    assert sm.credential_status("anthropic", env={"ANTHROPIC_API_KEY": "sk-x"}) == "measured"
    assert sm.credential_status("anthropic", env={}) == "unmeasured"
    # A foreign key does not make the cell measurable.
    assert sm.credential_status("anthropic", env={"OPENAI_API_KEY": "sk-y"}) == "unmeasured"
    assert sm.credential_status("openai", env={"OPENAI_API_KEY": "sk-y"}) == "measured"
    assert sm.credential_status("openai", env={}) == "unmeasured"
    # An empty string is absent, not present.
    assert sm.credential_status("openai", env={"OPENAI_API_KEY": ""}) == "unmeasured"


def test_bedrock_cell_is_measured_from_the_ambient_aws_chain_not_a_key() -> None:
    """Bedrock has no key of its own — its credential is 'boto3 resolves creds', probed here."""
    assert sm.credential_status("bedrock", env={}, aws_probe=lambda: True) == "measured"
    assert sm.credential_status("bedrock", env={}, aws_probe=lambda: False) == "unmeasured"


def test_call_budget_cap_refuses_a_matrix_that_would_exceed_it() -> None:
    """The cap is enforced BEFORE any network object is constructed: an over-budget plan raises.

    A matrix whose planned call count exceeds the predeclared cap must refuse to start rather
    than burn budget and truncate; a plan at or under the cap proceeds.
    """
    planned = sm.planned_call_count(n_cells=3, n_variants=2, n_prompts=2, n_repeats=10)
    assert planned == 120

    # Under / at the cap: no raise.
    sm.enforce_call_budget(planned, cap=120)
    sm.enforce_call_budget(planned, cap=300)

    # Over the cap: refuse.
    with pytest.raises(sm.CallBudgetExceeded):
        sm.enforce_call_budget(planned, cap=119)

    # The default cap is the declared 300.
    assert sm.DEFAULT_CALL_BUDGET == 300


def test_cells_load_one_per_committed_provider_overlay() -> None:
    """The measurement cells are the committed provider overlays — anthropic, bedrock, openai."""
    providers_dir = _ROOT / ".github" / "llm-providers"
    cells = sm.load_cells(providers_dir)
    assert {c.provider for c in cells} == {"anthropic", "bedrock", "openai"}
    for c in cells:
        assert Path(c.config_file).name.endswith(".toml")


def test_baseline_workflow_is_dispatch_only_with_a_budget_input_and_scoped_secrets() -> None:
    """The committed workflow is manual-only, predeclares its budget, and scopes each key.

    Live measurement is billable and key-bearing, so the workflow must be ``workflow_dispatch``
    only (never push/PR/schedule), expose the call budget as a workflow input, and hand each
    provider arm only its own credential (guarded on ``matrix.provider``), mirroring
    external-integration.yml.
    """
    import yaml

    wf_path = _ROOT / ".github" / "workflows" / "structured-output-baseline.yml"
    assert wf_path.exists(), "the baseline measurement workflow must be committed"
    wf = yaml.safe_load(wf_path.read_text())

    # PyYAML parses the bare `on:` key as the boolean True.
    on = wf.get("on", wf.get(True))
    assert set(on) == {"workflow_dispatch"}, "manual dispatch ONLY — never push/PR/schedule"
    assert "max_calls" in on["workflow_dispatch"]["inputs"], "budget must be a declared input"

    text = wf_path.read_text()
    # Per-arm secret scoping, exactly as external-integration.yml does it.
    assert "matrix.provider == 'anthropic' && secrets.ANTHROPIC_API_KEY" in text
    assert "matrix.provider == 'openai' && secrets.OPENAI_API_KEY" in text
    # The raw-reply + score bundle is uploaded as the operator-facing evidence.
    assert "upload-artifact" in text
