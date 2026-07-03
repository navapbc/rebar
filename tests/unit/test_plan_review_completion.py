"""The SEPARATE Pass-2 completion sub-call (epic 66ac / child 94fd).

Completion-aware container plan-review classifies each finding on three atomic axes (attribution /
containment / layer) so the LATER Pass-3 completion floor can drop findings that merely re-litigate
already-DELIVERED child work. These tests pin the MECHANISM this story builds — the sub-call and its
manifest assembler — with a ``FakeRunner`` (no live LLM):

* the registered contract's shape + fail-safe defaults;
* enum coercion to the closed vocabulary (invalid/missing → fail-safe default);
* the DETERMINISM rule: a ``_container_child`` (G3/G4 structural) finding gets its attribution set
  deterministically — the model is NOT asked to re-derive it — while non-structural findings pass
  the model's values through;
* the DEGRADE path: a sub-call error (or empty findings/manifest) → ``{}`` (the floor drops
  nothing);
* the manifest assembler picks ONLY delivered children (``delivered_now`` monkeypatched) and
  extracts each child's AC text.
"""

from __future__ import annotations

import pytest

from rebar.llm import contracts
from rebar.llm.config import LLMConfig
from rebar.llm.plan_review import attest, orchestrator, passes
from rebar.llm.runner import FakeRunner

pytestmark = pytest.mark.unit

_FAILSAFE = {
    "attribution": "none",
    "containment": "spans-open-or-system",
    "layer": "delivered-functionality",
}


def _cfg() -> LLMConfig:
    return LLMConfig(runner="fake")


def _run(fr, findings, manifest):
    return passes.pass2_completion(
        fr, _cfg(), plan="p", findings=findings, delivered_manifest=manifest
    )


class _BoomRunner:
    """A runner whose structured call RAISES — the degrade seam."""

    name = "boom"

    def preflight(self) -> None:  # pragma: no cover - trivial
        pass

    def run(self, req):
        raise RuntimeError("completion sub-call turn failed")


# ── the registered contract: shape + fail-safe defaults ───────────────────────────────────────
def test_completion_contract_registered_distinctly() -> None:
    comp = contracts.response_model_for("plan_review_completion")
    nov = contracts.response_model_for("plan_review_novelty")
    assert comp.__name__ == "CompletionOutput"
    assert comp.__name__ != nov.__name__  # distinct from the novelty shape
    # a partially-filled item defaults to the fail-safe (drop-nothing) values
    item = comp(completions=[{"index": 0}]).completions[0]
    assert item.attribution == "none"
    assert item.containment == "spans-open-or-system"
    assert item.layer == "delivered-functionality"


def test_closed_vocabulary_constants() -> None:
    # The closed enums the coercion enforces (frozen wording — shared with the prompt).
    assert passes.COMPLETION_CONTAINMENT == ("limited-to-closed", "spans-open-or-system", "n-a")
    assert passes.COMPLETION_LAYER == ("plan-semantics", "delivered-functionality", "n-a")
    assert passes.COMPLETION_ATTRIBUTION_NONE == "none"


# ── pass2_completion: enum coercion + fail-safe ────────────────────────────────────────────────
_MANIFEST = [{"ticket_id": "c1", "ac_text": "- [ ] the thing works"}]


def test_invalid_and_missing_enums_coerce_to_failsafe() -> None:
    findings = [
        {"finding": "f0", "criteria": ["G3"]},  # invalid enum values → coerce
        {"finding": "f1", "criteria": ["G4"]},  # missing enum keys → default
    ]
    fr = FakeRunner(
        structured={
            "completions": [
                {"index": 0, "attribution": "", "containment": "bogus", "layer": "??"},
                {"index": 1},  # no attribution/containment/layer at all
            ]
        }
    )
    out = _run(fr, findings, _MANIFEST)
    assert out[0] == _FAILSAFE  # blank attribution + 'bogus'/'??' enums all coerced
    assert out[1] == _FAILSAFE  # every key absent → all defaults


def test_valid_enums_pass_through_including_n_a() -> None:
    findings = [{"finding": "f0", "criteria": ["G3"]}]
    fr = FakeRunner(
        structured={
            "completions": [
                {
                    "index": 0,
                    "attribution": "c1",
                    "containment": "limited-to-closed",
                    "layer": "n-a",
                }
            ]
        }
    )
    out = _run(fr, findings, _MANIFEST)
    assert out[0] == {"attribution": "c1", "containment": "limited-to-closed", "layer": "n-a"}


# ── DETERMINISM: a `_container_child` finding is attributed deterministically ───────────────────
def test_container_child_attribution_is_deterministic() -> None:
    # The structural finding carries `_container_child=c1`; even though the model returns a
    # DIFFERENT attribution for its index, pass2_completion uses the pre-attributed child id.
    findings = [
        {"finding": "structural", "criteria": ["G3"], "_container_child": "c1"},
        {"finding": "free", "criteria": ["E2"]},  # non-structural → model provides attribution
    ]
    fr = FakeRunner(
        structured={
            "completions": [
                {
                    "index": 0,
                    "attribution": "WRONG",  # must be IGNORED (deterministic override to c1)
                    "containment": "limited-to-closed",
                    "layer": "plan-semantics",
                },
                {
                    "index": 1,
                    "attribution": "c1",
                    "containment": "spans-open-or-system",
                    "layer": "delivered-functionality",
                },
            ]
        }
    )
    out = _run(fr, findings, _MANIFEST)
    # structural: attribution is the DETERMINISTIC child id (model's "WRONG" ignored); the model's
    # containment + layer are still taken.
    assert out[0] == {
        "attribution": "c1",
        "containment": "limited-to-closed",
        "layer": "plan-semantics",
    }
    # non-structural: the model's attribution is passed through.
    assert out[1]["attribution"] == "c1"


def test_listing_prestates_structural_and_asks_model_for_nonstructural() -> None:
    """The finding listing tells the model NOT to re-derive a structural attribution (it is
    pre-stated), and to answer attribution for a non-structural finding — independence of the
    deterministic axis by construction."""
    findings = [
        {"finding": "structural", "criteria": ["G3"], "_container_child": "c1"},
        {"finding": "free", "criteria": ["E2"]},
    ]
    listing = passes._completion_finding_listing(findings)
    assert "attribution: c1 (PRE-ATTRIBUTED" in listing
    assert "answer only containment + layer" in listing
    # the non-structural finding asks the model for the attribution
    assert "answer the delivered child id it is about, or 'none'" in listing


# ── DEGRADE: an error (or nothing to classify) → {} (the floor drops nothing) ──────────────────
def test_degrade_on_runner_error_returns_empty() -> None:
    findings = [{"finding": "f0", "criteria": ["G3"]}]
    assert _run(_BoomRunner(), findings, _MANIFEST) == {}


def test_empty_findings_or_manifest_returns_empty_without_calling_the_model() -> None:
    calls = []

    class _Recorder(FakeRunner):
        def run(self, req):
            calls.append(1)
            return super().run(req)

    fr = _Recorder(structured={"completions": []})
    assert _run(fr, [], _MANIFEST) == {}
    assert _run(fr, [{"finding": "f0", "criteria": ["G3"]}], []) == {}
    assert not calls  # the sub-call is never invoked when there is nothing to classify


def test_out_of_range_and_malformed_indices_are_dropped_but_findings_still_classified() -> None:
    findings = [{"finding": "f0", "criteria": ["G3"]}]
    fr = FakeRunner(
        structured={
            "completions": [
                {"no_index": True},  # malformed → dropped
                {"index": 9, "attribution": "c1"},  # out-of-range → dropped
            ]
        }
    )
    out = _run(fr, findings, _MANIFEST)
    # finding 0 got no valid answer → all fail-safe defaults (still classified, never omitted)
    assert out == {0: _FAILSAFE}


# ── the manifest assembler: only DELIVERED children, with AC text ───────────────────────────────
_AC = "## Acceptance Criteria\n- [ ] c-delivered works\n- [ ] and is verified\n"


def test_delivered_children_manifest_picks_only_delivered_and_extracts_ac(monkeypatch) -> None:

    children = [
        {"ticket_id": "c1", "status": "closed", "description": f"Some body.\n\n{_AC}"},
        {
            "ticket_id": "c2",
            "status": "open",
            "description": "## Acceptance Criteria\n- [ ] not done\n",
        },
    ]

    monkeypatch.setattr("rebar._reads.list_tickets", lambda *, parent, repo_root=None: children)

    def _show(cid, repo_root=None):
        return next(c for c in children if c["ticket_id"] == cid)

    monkeypatch.setattr("rebar._reads.show_ticket", _show)
    # Only c1 is delivered. orchestrator does `from . import attest` — patch the module it binds.
    monkeypatch.setattr(
        attest,
        "delivered_now",
        lambda child, siblings, repo_root=None: child.get("ticket_id") == "c1",
    )

    manifest = orchestrator.delivered_children_manifest("epic", repo_root=None)
    assert [m["ticket_id"] for m in manifest] == ["c1"]  # c2 (open) excluded
    assert manifest[0]["ac_text"] == "- [ ] c-delivered works\n- [ ] and is verified"


def test_delivered_children_manifest_empty_on_enumeration_error(monkeypatch) -> None:

    def boom(*, parent, repo_root=None):
        raise RuntimeError("store read failed")

    monkeypatch.setattr("rebar._reads.list_tickets", boom)
    assert orchestrator.delivered_children_manifest("epic", repo_root=None) == []


def test_extract_ac_section_stops_at_next_heading() -> None:
    desc = "## Why\nbecause\n\n## Acceptance Criteria\n- [ ] a\n- [ ] b\n\n## Scope\nthings\n"
    assert orchestrator._extract_ac_section(desc) == "- [ ] a\n- [ ] b"
    assert orchestrator._extract_ac_section("no ac section here") == ""
