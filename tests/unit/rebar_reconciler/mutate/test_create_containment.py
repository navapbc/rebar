"""Held-out behavioral oracle for REB-3115 S4 T2 — the pure-decision CREATE containment.

This oracle pins the OBSERVABLE contract of the new pure module

``rebar_reconciler.create_containment``
    ``contain_created(plan, known_key, *, record_key, attach_label, set_property,
    confirm)``
    — the post-create key slice. AFTER a create returns a Jira key it contains that key
    onto the durable pending binding in a FIXED write-ahead order (story 9622 / bug
    387d): ``record_key`` persists the key on the still-pending entry BEFORE any label,
    then ``attach_label`` attaches the canonical ``rebar-id`` label, then the OPTIONAL
    ``set_property`` enrichment, then ``confirm`` (``bind_confirm``) LAST. On ANY write
    failure it NEVER deletes the remote issue — the key is preserved on every abort so
    recovery can retro-attach the remaining containment. Its DECISION logic performs
    zero I/O and reads no clock — the four injected raise-based callables are the sole
    side-effect channels, so identical inputs yield equal outputs.

Assertions are OBSERVABLE ONLY (enums / buckets / flags / keys / recorded call order)
— never private names or source text — so a behavior-preserving refactor cannot break
them. Every one of the six T2 ACs is covered by at least one test; recording fakes
append each seam call to a shared list so the fixed ordering is asserted directly, and
Cloud/DC provider labels show the containment is provider-neutral.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

if "rebar_reconciler" not in sys.modules:  # pragma: no cover - import bootstrap
    _pkg = types.ModuleType("rebar_reconciler")
    _pkg.__path__ = [str(RECON_DIR)]
    sys.modules["rebar_reconciler"] = _pkg


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def outcome_mod():
    return _load("operation_outcome_containment_test", "operation_outcome.py")


@pytest.fixture(scope="module")
def policy_mod():
    return _load("failure_policy_containment_test", "failure_policy.py")


@pytest.fixture(scope="module")
def containment_mod():
    return _load("create_containment_containment_test", "create_containment.py")


# ── Deterministic, injected test doubles ─────────────────────────────────────────


class _Plan:
    """A minimal create ticket plan carrying only what the containment reads: an
    ``.identity`` (mirrors how the sibling create coordinator reads ``plan.identity``)."""

    def __init__(self, identity: str) -> None:
        self.identity = identity


def _plan(identity: str = "REB-NEW") -> _Plan:
    return _Plan(identity)


class _RecordingSeam:
    """An injected ``seam(plan, known_key)`` double. It APPENDS its label to the shared
    ``order`` list on every call so the fixed write-ahead ordering is asserted directly,
    and RAISES the scripted error when one is configured."""

    def __init__(self, name: str, order: list[str], *, raises: Exception | None = None) -> None:
        self.name = name
        self._order = order
        self._raises = raises
        self.calls: list[tuple[str, str]] = []

    def __call__(self, plan, known_key) -> None:
        self._order.append(self.name)
        self.calls.append((plan.identity, known_key))
        if self._raises is not None:
            raise self._raises


def _seams(order: list[str], *, faulty: str | None = None, error: Exception | None = None):
    """Build the four recording seams sharing one ``order`` list, optionally scripting a
    fault at a single stage."""
    exc = error or RuntimeError(f"{faulty} failed")
    return {
        name: _RecordingSeam(name, order, raises=exc if name == faulty else None)
        for name in ("record_key", "attach_label", "set_property", "confirm")
    }


# ════════════════════════════════════════════════════════════════════════════════
# AC1 — fixed write-ahead ORDER on full success: record_key → attach_label →
#       set_property → confirm. Key-before-label and label-before-property/confirm.
# ════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("venue", ["cloud", "dc"])
def test_ac1_full_success_records_fixed_order(containment_mod, outcome_mod, policy_mod, venue):
    D = outcome_mod.Disposition
    S = outcome_mod.FailureScope
    order: list[str] = []
    seams = _seams(order)

    outcome = containment_mod.contain_created(_plan(f"REB-{venue}"), f"{venue}-1", **seams)

    assert order == ["record_key", "attach_label", "set_property", "confirm"]
    assert order.index("record_key") < order.index("attach_label")
    assert order.index("attach_label") < order.index("set_property")
    assert order.index("attach_label") < order.index("confirm")
    assert outcome.disposition == D.applied
    assert outcome.failure_scope == S.none
    assert outcome.bucket == "applied"
    assert outcome.bucket == policy_mod.bucket_for(D.applied)
    assert outcome.known_key == f"{venue}-1"
    assert outcome.label_attached is True
    assert outcome.property_attached is True
    assert outcome.confirmed is True
    assert outcome.diagnostics == ()


# ════════════════════════════════════════════════════════════════════════════════
# AC2 — a set_property failure stays VISIBLE and never removes the already-attached
#       label; safety_aborted, not confirmed, key still known.
# ════════════════════════════════════════════════════════════════════════════════


def test_ac2_property_failure_visible_label_preserved(containment_mod, outcome_mod, policy_mod):
    D = outcome_mod.Disposition
    S = outcome_mod.FailureScope
    order: list[str] = []
    seams = _seams(order, faulty="set_property", error=RuntimeError("property API down"))

    outcome = containment_mod.contain_created(_plan(), "REB-900", **seams)

    assert outcome.disposition == D.safety_aborted
    assert outcome.failure_scope == S.ticket
    assert outcome.bucket == "deferred"
    assert outcome.bucket == policy_mod.bucket_for(D.safety_aborted)
    # Label was attached and is NOT removed by the property failure.
    assert outcome.label_attached is True
    assert outcome.property_attached is False
    assert outcome.confirmed is False
    assert outcome.has_known_key() is True
    assert outcome.known_key == "REB-900"
    # The property failure is present and visible as an enrichment diagnostic.
    stages = [d for d in outcome.diagnostics if d.get("stage") == "set_property"]
    assert len(stages) == 1
    assert stages[0].get("category") == "enrichment"
    assert "property API down" in stages[0].get("message", "")
    # confirm never fired after the property abort.
    assert order == ["record_key", "attach_label", "set_property"]


# ════════════════════════════════════════════════════════════════════════════════
# AC3 — facade-only persistence: the ONLY persistence/confirmation channels are the
#       injected seams; a successful run confirms EXACTLY once via the injected seam.
# ════════════════════════════════════════════════════════════════════════════════


def test_ac3_persistence_and_confirm_only_through_injected_seams(containment_mod):
    order: list[str] = []
    seams = _seams(order)

    outcome = containment_mod.contain_created(_plan("REB-FAC"), "REB-FAC-1", **seams)

    # record_key (persistence) and confirm both fired through the injected callables.
    assert seams["record_key"].calls == [("REB-FAC", "REB-FAC-1")]
    assert seams["confirm"].calls == [("REB-FAC", "REB-FAC-1")]
    # Confirmation happened EXACTLY once — no other write path exists.
    assert order.count("confirm") == 1
    assert order.count("record_key") == 1
    assert outcome.confirmed is True


# ════════════════════════════════════════════════════════════════════════════════
# AC4 — a fault at ANY post-create stage still carries the known key, is
#       safety_aborted / "deferred", and retains a stage diagnostic (recovery evidence).
# ════════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("faulty", ["record_key", "attach_label", "set_property", "confirm"])
def test_ac4_every_post_create_failure_keeps_known_key(
    containment_mod, outcome_mod, policy_mod, faulty
):
    D = outcome_mod.Disposition
    S = outcome_mod.FailureScope
    order: list[str] = []
    seams = _seams(order, faulty=faulty)

    outcome = containment_mod.contain_created(_plan("REB-K"), "REB-K-42", **seams)

    assert outcome.has_known_key() is True
    assert outcome.known_key == "REB-K-42"
    assert outcome.disposition == D.safety_aborted
    assert outcome.failure_scope == S.ticket
    assert outcome.bucket == "deferred"
    assert outcome.bucket == policy_mod.bucket_for(D.safety_aborted)
    # Recovery evidence: a diagnostic naming the exact failed stage is retained.
    stages = {d.get("stage") for d in outcome.diagnostics}
    assert faulty in stages
    assert outcome.confirmed is False


# ════════════════════════════════════════════════════════════════════════════════
# AC5 — no remote delete: the entry point exposes NO delete/undo/rollback/unbind seam;
#       its keyword-only seams are exactly the four containment writes.
# ════════════════════════════════════════════════════════════════════════════════


def test_ac5_no_delete_injectable_exists(containment_mod):
    sig = inspect.signature(containment_mod.contain_created)
    kw_only = {
        name for name, p in sig.parameters.items() if p.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert kw_only == {"record_key", "attach_label", "set_property", "confirm"}
    assert not any(
        tok in name for name in kw_only for tok in ("delete", "undo", "rollback", "unbind")
    )


# ════════════════════════════════════════════════════════════════════════════════
# AC6 — confirmation gates later intents: ``confirmed`` is True ONLY on the fully
#       successful path; on EVERY abort it is False (so a caller gating downstream
#       lifecycle intents on ``confirmed`` holds them).
# ════════════════════════════════════════════════════════════════════════════════


def test_ac6_confirmed_true_only_on_full_success(containment_mod):
    order: list[str] = []
    ok = containment_mod.contain_created(_plan(), "REB-OK", **_seams(order))
    assert ok.confirmed is True


@pytest.mark.parametrize("faulty", ["record_key", "attach_label", "set_property", "confirm"])
def test_ac6_confirmed_false_on_every_abort(containment_mod, faulty):
    order: list[str] = []
    outcome = containment_mod.contain_created(_plan(), "REB-ABORT", **_seams(order, faulty=faulty))
    assert outcome.confirmed is False


def test_ac6_confirm_failure_leaves_confirmed_false_despite_label_and_property(containment_mod):
    order: list[str] = []
    outcome = containment_mod.contain_created(_plan(), "REB-CF", **_seams(order, faulty="confirm"))
    # label + property both succeeded, but the final confirm did not commit.
    assert outcome.label_attached is True
    assert outcome.property_attached is True
    assert outcome.confirmed is False
    assert outcome.has_known_key() is True


# ════════════════════════════════════════════════════════════════════════════════
# Determinism — identical inputs yield an EQUAL ContainmentOutcome (pure decision core).
# ════════════════════════════════════════════════════════════════════════════════


def test_pure_decision_is_deterministic(containment_mod):
    def run():
        return containment_mod.contain_created(_plan("REB-DET"), "REB-DET-1", **_seams([]))

    assert run() == run()
