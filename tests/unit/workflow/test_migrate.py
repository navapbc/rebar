"""Unit tests for the workflow schema_version migration shim (WS-B3).

v1 is the base DSL version, so there is no real shim to round-trip yet. These
tests prove the chaining machinery + the upgrade-rebar gate, and register a
SYNTHETIC shim to demonstrate the golden round-trip discipline every future shim
must follow.
"""

from __future__ import annotations

import pytest

from rebar.llm.errors import WorkflowVersionError
from rebar.llm.workflow import migrate as mig
from rebar.llm.workflow import schema as wf


def test_current_version_is_identity() -> None:
    # A document already at the CURRENT version (v2) migrates to itself (a copy).
    doc = {"schema_version": "2", "name": "x", "steps": [{"id": "s", "uses": "u"}]}
    out = mig.migrate_to_current(doc)
    assert out == doc
    assert out is not doc  # a copy, never the same object


def test_does_not_mutate_input() -> None:
    # Up-converting v1->v2 must not mutate the caller's document.
    doc = {"schema_version": "1", "name": "x", "steps": [{"id": "s", "uses": "u"}]}
    mig.migrate_to_current(doc)
    assert doc == {"schema_version": "1", "name": "x", "steps": [{"id": "s", "uses": "u"}]}


def test_v1_to_v2_golden_roundtrip() -> None:
    # The golden round-trip for the real v1->v2 shim (WS-B3 discipline): a v1
    # fixture and its pinned v2 output. v2 is a strict superset of v1, so the shim
    # is a pure version bump — the steps are byte-identical, only schema_version
    # advances. (The chaining machinery + upgrade gate are covered by the synthetic
    # tests below; this pins the ACTUAL conversion.)
    v1 = {
        "schema_version": "1",
        "name": "demo",
        "inputs": {"ticket_id": {"type": "string", "required": True}},
        "steps": [
            {
                "id": "fetch",
                "uses": "fetch_ticket",
                "with": {"ticket_id": "${{ inputs.ticket_id }}"},
            },
            {"id": "review", "prompt": "code-quality", "needs": ["fetch"]},
        ],
    }
    expected_v2 = {**v1, "schema_version": "2"}
    out = mig.migrate_to_current(v1)
    assert out == expected_v2
    # The lone difference is the version stamp; everything else round-trips verbatim.
    assert {k: v for k, v in out.items() if k != "schema_version"} == {
        k: v for k, v in v1.items() if k != "schema_version"
    }
    assert mig._v1_to_v2({"schema_version": "1", "name": "x", "steps": []}) == {
        "schema_version": "2",
        "name": "x",
        "steps": [],
    }


def test_v1_to_v2_is_registered() -> None:
    # v1 has a registered up-conversion path now that v2 is the current version.
    assert mig.registered_source_versions() == ("1",)
    assert "1" in mig._SHIMS


def test_migrated_v1_validates_against_the_v2_schema() -> None:
    # The shim's whole CONTRACT: the up-converted document is a VALID v2 file (not just
    # a version-stamped v1) — validated against the real v2 JSON Schema.
    v1 = {
        "schema_version": "1",
        "name": "demo",
        "inputs": {"ticket_id": {"type": "string", "required": True}},
        "steps": [
            {"id": "fetch", "uses": "fetch_ticket",
             "with": {"ticket_id": "${{ inputs.ticket_id }}"}},
            {"id": "review", "prompt": "code-quality", "needs": ["fetch"]},
        ],
    }
    out = mig.migrate_to_current(v1)
    schema_errors = [e for e in wf.validate_document(out) if not e.startswith("note:")]
    assert schema_errors == [], schema_errors


def test_newer_version_is_upgrade_error() -> None:
    with pytest.raises(WorkflowVersionError, match="upgrade rebar"):
        mig.migrate_to_current({"schema_version": "999", "name": "x", "steps": []})


def test_no_shim_path_is_clear_error(monkeypatch) -> None:
    # Pretend v2 is the current build but provide no v1->v2 shim: an older
    # supported file must fail with a located, actionable error (not a hang).
    monkeypatch.setattr(wf, "CURRENT_SCHEMA_VERSION", "2")
    monkeypatch.setattr(mig, "CURRENT_SCHEMA_VERSION", "2")
    monkeypatch.setattr(wf, "SUPPORTED_SCHEMA_VERSIONS", ("1", "2"))
    monkeypatch.setattr(mig, "SUPPORTED_SCHEMA_VERSIONS", ("1", "2"))
    monkeypatch.setattr(mig, "_SHIMS", {})
    with pytest.raises(WorkflowVersionError, match="no migration shim"):
        mig.migrate_to_current({"schema_version": "1", "name": "x", "steps": []})


def test_synthetic_shim_chains_v1_to_v3(monkeypatch) -> None:
    # Stand up a synthetic v3 world with two shims and prove the chain composes
    # deterministically, advancing the version one step at a time. This is the
    # template for a real shim's golden round-trip test.
    monkeypatch.setattr(wf, "CURRENT_SCHEMA_VERSION", "3")
    monkeypatch.setattr(mig, "CURRENT_SCHEMA_VERSION", "3")
    monkeypatch.setattr(wf, "SUPPORTED_SCHEMA_VERSIONS", ("1", "2", "3"))
    monkeypatch.setattr(mig, "SUPPORTED_SCHEMA_VERSIONS", ("1", "2", "3"))

    def v1_to_v2(doc):
        out = dict(doc)
        out["added_in_v2"] = True
        return out

    def v2_to_v3(doc):
        out = dict(doc)
        out["added_in_v3"] = True
        return out

    monkeypatch.setattr(mig, "_SHIMS", {"1": v1_to_v2, "2": v2_to_v3})

    out = mig.migrate_to_current({"schema_version": "1", "name": "x", "steps": []})
    assert out["schema_version"] == "3"
    assert out["added_in_v2"] is True
    assert out["added_in_v3"] is True
    assert mig.registered_source_versions() == ("1", "2")


def test_shim_that_forgets_version_is_corrected(monkeypatch) -> None:
    # A buggy shim that forgets to advance schema_version must not loop forever —
    # migrate stamps the target version defensively.
    monkeypatch.setattr(wf, "CURRENT_SCHEMA_VERSION", "2")
    monkeypatch.setattr(mig, "CURRENT_SCHEMA_VERSION", "2")
    monkeypatch.setattr(wf, "SUPPORTED_SCHEMA_VERSIONS", ("1", "2"))
    monkeypatch.setattr(mig, "SUPPORTED_SCHEMA_VERSIONS", ("1", "2"))
    monkeypatch.setattr(mig, "_SHIMS", {"1": lambda doc: dict(doc)})  # no version bump
    out = mig.migrate_to_current({"schema_version": "1", "name": "x", "steps": []})
    assert out["schema_version"] == "2"
