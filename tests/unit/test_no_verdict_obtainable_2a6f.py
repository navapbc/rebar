"""Bug 2a6f — a completion FAIL that names no criterion is a VERIFIER FAULT, not a judgement.

``reconcile_verdict`` coerces anything that is not exactly ``PASS`` to ``FAIL``. When the
verifier's structured turn came back garbled/truncated (or without a ``verdict`` at all) the
result was a FAIL with an empty ``findings``, and the reconciler INVENTED a placeholder
criterion ``(unspecified)`` for it. In the field that produced:

    Error: completion verification FAILED for cb91-… — 1 unmet criteria; not closing.
      - (unspecified): verifier returned FAIL without itemizing the failing criterion.

— a block with no remediation path, indistinguishable from a genuine one-criterion failure.
These tests pin the replacement: recover real criteria from the positive manifest when it
names any, else mark ``verdict_obtainable=False`` so callers can tell a fault from a verdict.
The ``{PASS, FAIL}`` vocabulary is unchanged and the verdict still blocks fail-closed.
"""

from __future__ import annotations


def test_bare_fail_is_marked_a_fault_not_a_fabricated_criterion() -> None:
    from rebar.llm.completion import reconcile_verdict

    result: dict = {"verdict": "FAIL", "findings": []}
    reconcile_verdict(result)

    assert result["verdict"] == "FAIL"  # still fail-closed
    assert result["verdict_obtainable"] is False
    assert len(result["findings"]) == 1
    finding = result["findings"][0]
    assert finding["criterion"] == "(no verdict obtainable)"
    # The old placeholder text asserted a failing criterion existed; the replacement says the
    # opposite — nothing was evaluated — and tells the caller to retry.
    assert "VERIFIER FAULT" in finding["detail"]
    assert "not\nevidence" in finding["detail"] or "not evidence" in finding["detail"]
    assert "(unspecified)" not in finding["criterion"]


def test_missing_verdict_key_lands_in_the_fault_channel() -> None:
    """A structured turn that produced no ``verdict`` at all normalizes to FAIL — which must
    be reported as a fault, not as an unmet criterion."""
    from rebar.llm.completion import reconcile_verdict

    result: dict = {"findings": []}
    reconcile_verdict(result)

    assert result["verdict"] == "FAIL"
    assert result["verdict_obtainable"] is False


def test_unmet_criteria_are_recovered_from_the_manifest_before_faulting() -> None:
    """The workflow path populates ``criteria`` BEFORE delegating to ``reconcile_verdict``, so
    a bare FAIL can arrive carrying a manifest. When that manifest names unmet criteria they are
    real findings — recovering them beats reporting a fault, and beats the old placeholder."""
    from rebar.llm.completion import reconcile_verdict

    result: dict = {
        "verdict": "FAIL",
        "findings": [],
        "criteria": [
            {"criterion": "ships a --json flag", "met": True},
            {"criterion": "documents the flag in the user guide", "met": False},
            {"criterion": "adds a regression test", "met": False},
        ],
    }
    reconcile_verdict(result)

    assert result["verdict"] == "FAIL"
    # A real judgement, so NOT a fault.
    assert "verdict_obtainable" not in result
    assert [f["criterion"] for f in result["findings"]] == [
        "documents the flag in the user guide",
        "adds a regression test",
    ]


def test_manifest_with_no_unmet_entries_still_faults() -> None:
    """A manifest that names nothing unmet cannot explain a FAIL — that is still a fault."""
    from rebar.llm.completion import reconcile_verdict

    result: dict = {
        "verdict": "FAIL",
        "findings": [],
        "criteria": [{"criterion": "ships a --json flag", "met": True}],
    }
    reconcile_verdict(result)

    assert result["verdict_obtainable"] is False
    assert result["findings"][0]["criterion"] == "(no verdict obtainable)"


def test_genuine_itemized_fail_is_untouched() -> None:
    """The common case must not acquire the marker — otherwise every real FAIL would be
    reported as retryable and the close gate would stop being informative."""
    from rebar.llm.completion import reconcile_verdict

    result: dict = {
        "verdict": "FAIL",
        "findings": [{"criterion": "adds a regression test", "detail": "no test found"}],
    }
    reconcile_verdict(result)

    assert result["verdict"] == "FAIL"
    assert "verdict_obtainable" not in result
    assert result["findings"][0]["criterion"] == "adds a regression test"


def test_pass_carries_no_fault_marker() -> None:
    from rebar.llm.completion import reconcile_verdict

    result: dict = {"verdict": "PASS", "findings": []}
    reconcile_verdict(result)

    assert result["verdict"] == "PASS"
    assert "verdict_obtainable" not in result


def test_reconcile_is_idempotent_over_the_marker() -> None:
    """The sidecar re-reconciles an already-reconciled verdict; a second pass must not flip a
    recovered fault back or duplicate its finding."""
    from rebar.llm.completion import reconcile_verdict

    result: dict = {"verdict": "FAIL", "findings": []}
    reconcile_verdict(result)
    first = [dict(f) for f in result["findings"]]
    reconcile_verdict(result)

    assert result["verdict_obtainable"] is False
    assert result["findings"] == first


def test_workflow_path_inherits_the_marker() -> None:
    """``gate_ops.completion_reconcile`` (the close gate's path, which also stamps
    ``certifiable``) delegates to ``reconcile_verdict`` — assert it actually inherits the
    marker rather than assuming delegation."""
    import inspect

    from rebar.llm.workflow import gate_ops

    src = inspect.getsource(gate_ops.completion_reconcile)
    assert "reconcile_verdict(result)" in src


def test_sidecar_carries_the_marker_onto_the_durable_record() -> None:
    from rebar.llm.completion_sidecar import build_payload

    payload = build_payload({"verdict": "FAIL", "findings": [], "ticket_id": "abcd"})
    assert payload["verdict_obtainable"] is False

    ordinary = build_payload(
        {
            "verdict": "FAIL",
            "findings": [{"criterion": "x", "detail": "y"}],
            "ticket_id": "abcd",
        }
    )
    assert "verdict_obtainable" not in ordinary
