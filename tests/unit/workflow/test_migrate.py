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
    doc = {"schema_version": "1", "name": "x", "steps": [{"id": "s", "uses": "u"}]}
    out = mig.migrate_to_current(doc)
    assert out == doc
    assert out is not doc  # a copy, never the same object


def test_does_not_mutate_input() -> None:
    doc = {"schema_version": "1", "name": "x", "steps": [{"id": "s", "uses": "u"}]}
    mig.migrate_to_current(doc)
    assert doc == {"schema_version": "1", "name": "x", "steps": [{"id": "s", "uses": "u"}]}


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
