"""The usage log's step attribution: the writer contract and its inertness (story b690).

The END-TO-END proof that a step id ARRIVES lives in
``tests/unit/workflow/test_code_review_usage_attribution.py``, which asserts against a log a real
gate run produced — reading the writer cannot show that. What is pinned HERE is the writer's own
contract, which that test cannot isolate: the omission rule, the class-token filter, the
ContextVar's scoping, and the guarantee that all of this stays inert when the log is not configured.
"""

from __future__ import annotations

import json

import pytest

from rebar.llm import usage_log

pytestmark = pytest.mark.unit

_USAGE = {"input_tokens": 10, "output_tokens": 3, "requests": 1}


def _rows(path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# ── inertness: the whole feature is a no-op unless REBAR_USAGE_LOG is set ────────────────────


def test_record_is_a_no_op_when_the_log_is_not_configured(monkeypatch, tmp_path):
    """Every normal library/test run takes this path, so it must write nothing at all — the
    reason `make test` is byte-unchanged by this feature."""
    monkeypatch.delenv("REBAR_USAGE_LOG", raising=False)
    with usage_log.step_identity("verify", "standard"):
        usage_log.record(_USAGE, op="p", model="m", step="verify", model_class="standard")
    assert not list(tmp_path.iterdir())


def test_record_with_no_usage_writes_nothing(monkeypatch, tmp_path):
    log = tmp_path / "u.jsonl"
    monkeypatch.setenv("REBAR_USAGE_LOG", str(log))
    usage_log.record({}, op="p", step="verify")
    assert not log.exists()


# ── the writer contract: additive, and absent values are OMITTED not nulled ──────────────────


def test_record_writes_step_and_model_class(monkeypatch, tmp_path):
    log = tmp_path / "u.jsonl"
    monkeypatch.setenv("REBAR_USAGE_LOG", str(log))
    usage_log.record(
        _USAGE, op="code-review-verify", model="m", step="verify", model_class="standard"
    )
    (row,) = _rows(log)
    assert row["step"] == "verify"
    assert row["model_class"] == "standard"
    assert row["op"] == "code-review-verify"  # the existing field is untouched


def test_absent_step_and_class_are_omitted_rather_than_written_as_null(monkeypatch, tmp_path):
    """A row without `step` means "this call was not made inside a workflow step" — the truthful
    answer for a spec scan or an enrich pass. Following `model`/`provider`'s existing pattern, so
    a reader never has to distinguish absent-from-null."""
    log = tmp_path / "u.jsonl"
    monkeypatch.setenv("REBAR_USAGE_LOG", str(log))
    usage_log.record(_USAGE, op="spec-scan", model="m")
    (row,) = _rows(log)
    assert "step" not in row and "model_class" not in row


def test_summarize_folds_a_log_written_by_the_new_writer(monkeypatch, tmp_path):
    """The additive requirement: `summarize()` feeds `$GITHUB_STEP_SUMMARY` on the billable CI
    jobs, so it must still fold a file containing the new fields."""
    log = tmp_path / "u.jsonl"
    monkeypatch.setenv("REBAR_USAGE_LOG", str(log))
    usage_log.record(_USAGE, op="a", model="m", step="base", model_class="frontier")
    usage_log.record(_USAGE, op="b", model="m")  # a row with neither new field
    out = usage_log.summarize(str(log))
    assert "a" in out and "b" in out
    assert out != "No LLM calls recorded."


# ── the carrier: what the step executor binds, and that it does not leak ─────────────────────


def test_active_step_is_none_outside_any_step():
    assert usage_log.active_step() is None


def test_step_identity_binds_and_then_drops(monkeypatch):
    with usage_log.step_identity("round_a", "frontier"):
        assert usage_log.active_step() == ("round_a", "frontier")
    assert usage_log.active_step() is None, "the step identity leaked past its own scope"


def test_step_identity_restores_the_enclosing_step_not_none():
    """Nesting must restore the OUTER step, which is why this is a token reset and not a clear."""
    with usage_log.step_identity("outer", "frontier"):
        with usage_log.step_identity("inner", "standard"):
            assert usage_log.active_step() == ("inner", "standard")
        assert usage_log.active_step() == ("outer", "frontier")


def test_step_identity_drops_even_when_the_step_raises():
    with pytest.raises(RuntimeError):
        with usage_log.step_identity("boom", "frontier"):
            raise RuntimeError("step failed")
    assert usage_log.active_step() is None


# ── the class filter: only a reserved class name is recorded AS a class ──────────────────────


@pytest.mark.parametrize("token", ["trivial", "standard", "frontier"])
def test_a_reserved_class_name_is_recognised_as_a_class(token):
    assert usage_log.declared_model_class(token) == token


@pytest.mark.parametrize(
    "token",
    [
        None,
        "",
        "claude-opus-4-8",
        "anthropic:claude-sonnet-4-6",
        "bedrock:us.anthropic.claude-haiku-4-5",
        "Standard",  # class names are reserved words, not a case-insensitive match
    ],
)
def test_a_literal_model_id_is_not_recorded_as_a_class(token):
    """Recording a literal id as a class would recreate the exact ambiguity this attribution
    exists to remove: "resolved to opus because frontier" vs "because cfg.model"."""
    assert usage_log.declared_model_class(token) is None


def test_the_class_filter_tracks_the_reserved_vocabulary_rather_than_a_copy_of_it():
    """A fourth class must not need a second edit here — the filter reads CLASS_NAMES itself."""
    from rebar.llm.model_classes import CLASS_NAMES

    for name in CLASS_NAMES:
        assert usage_log.declared_model_class(name) == name
