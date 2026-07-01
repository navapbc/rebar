"""WS3 (epic b744): the code-review gate workflow + the two NOVEL ops.

Pins, OFFLINE (no tokens): overlay_union's (glob ∪ recommend) − already_run with cap + one-hop;
merge_findings clustering; code_review_decide determinism (escalation can NEVER change the
verdict for an identical finding set); and the gate workflow running end-to-end with Round-B
membership == the escalated set, the verdict invariant under escalation.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml

from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.workflow import executor as _ex
from rebar.llm.workflow import migrate as _migrate
from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the code_review ops
from rebar.llm.workflow.runners import AgentStepRunner

pytestmark = pytest.mark.unit

_GATE = pathlib.Path("src/rebar/llm/workflow/gates/code-review.yaml")


def _ctx(step, inputs):
    return _ex.StepContext(
        run_id="r", step_id="s", kind="uses", step=step, inputs=inputs, workflow={}, repo_root=None
    )


def _run_op(name, inputs):
    return _ex.STEP_REGISTRY[name](_ctx({"uses": name}, inputs))


# ── overlay_union (the NOVEL escalation op) ─────────────────────────────────────────────────
def test_triggers_call_is_the_glob_set():
    # The Round-A `triggers` call: recommend/already_run default empty → to_run == glob set.
    out = _run_op("overlay_union", {"changed_files": ["src/auth/login.py", "docs/x.md"]})
    assert set(out["to_run"]) == {"security", "docs"}  # auth* → security, docs/** → docs
    assert out["include_security"] is True and out["include_docs"] is True
    assert out["include_tests"] is False


def test_union_escalates_minus_already_run_one_hop():
    # The Round-B `union` call: (glob ∪ base.recommend) − already_run(Round-A).
    out = _run_op(
        "overlay_union",
        {
            "changed_files": ["src/auth/login.py"],  # glob → {security}
            "recommend": [
                {"overlay_id": "tests", "reason": "no test"},
                {"overlay_id": "performance", "reason": "hot"},
            ],
            "already_run": ["security"],  # Round-A already ran security
        },
    )
    # security was already run (one-hop bound) → only the freshly-escalated overlays remain
    assert set(out["to_run"]) == {"tests", "performance"}
    assert out["include_security"] is False
    assert out["include_tests"] is True and out["include_performance"] is True


def test_union_caps_fanout():
    out = _run_op(
        "overlay_union",
        {
            "changed_files": [],
            "recommend": [
                {"overlay_id": o, "reason": "x"}
                for o in ("security", "performance", "i18n", "a11y", "docs")
            ],
            "already_run": [],
            "cap": 3,
        },
    )
    assert len(out["to_run"]) == 3  # capped


def test_union_drops_out_of_enum_recommend():
    out = _run_op(
        "overlay_union",
        {
            "changed_files": [],
            "recommend": [{"overlay_id": "made-up", "reason": "x"}],
            "already_run": [],
        },
    )
    assert out["to_run"] == []  # bogus id dropped by the enum filter


# ── merge_findings clustering ───────────────────────────────────────────────────────────────
def test_merge_clusters_findings_on_one_location():
    base = [
        {
            "finding": "bug here",
            "criteria": ["security"],
            "location": "a.py:10",
            "evidence": ["e1"],
            "reviewer_id": "base",
        }
    ]
    # three overlays flag the SAME location+criterion within the line bucket → collapse to one
    overlays = [
        {
            "finding": "same spot",
            "criteria": ["security"],
            "location": "a.py:12",
            "evidence": ["e2"],
            "reviewer_id": "code-review-security",
        },
        {
            "finding": "same spot again",
            "criteria": ["security"],
            "location": "a.py:9",
            "evidence": ["e3"],
            "reviewer_id": "code-review-base",
        },
    ]
    out = _run_op(
        "merge_findings",
        {"base_findings": base, "round_a_findings": overlays, "round_b_findings": []},
    )
    assert out["merged_count"] == 3
    assert out["clustered_count"] == 1  # all three collapse (same file+criterion, within 10 lines)
    rep = out["findings"][0]
    assert rep["agreement"] == 3
    assert set(rep["evidence"]) == {"e1", "e2", "e3"}  # evidence unioned
    assert rep["id"] == "0"
    # collapsing is NON-LOSSY: the two non-representative members' text is preserved.
    assert set(rep["merged_from"]) == {"same spot", "same spot again"}


def test_merge_does_not_collapse_distinct_findings_lacking_a_criterion():
    # two distinct issues at the same location but with NO criterion must NOT collapse by
    # coincidence (location-anchored clustering requires BOTH a path and a criterion).
    out = _run_op(
        "merge_findings",
        {
            "sources": [
                [{"finding": "first issue", "criteria": [], "location": "a.py:10"}],
                [{"finding": "second different issue", "criteria": [], "location": "a.py:11"}],
            ]
        },
    )
    assert out["clustered_count"] == 2


def test_merge_normalizes_a_missing_or_nonstring_finding_key():
    # a contract-violating finding with a missing / null / non-string `finding` (or null
    # `criteria`) must not crash downstream — coach_listing does `f['finding'][:200]` and the
    # kernel iterates `criteria`. merge_findings coerces both to safe types.
    out = _run_op(
        "merge_findings",
        {
            "sources": [
                [{"criteria": ["security"], "location": "a.py:1"}],  # missing finding
                [{"finding": None, "criteria": ["tests"], "location": "b.py:1"}],  # null value
                [
                    {"finding": 123, "criteria": None, "location": "c.py:1"}
                ],  # non-string + null criteria
            ]
        },
    )
    for f in out["findings"]:
        assert isinstance(f["finding"], str)
        assert isinstance(f["criteria"], list)
        # the coerced fields are safe for the kernel's `f['finding'][:200]` + `for c in criteria`
        _ = f["finding"][:200]
        assert all(isinstance(c, str) for c in f["criteria"])


def test_merge_keeps_distinct_dimensions_separate():
    out = _run_op(
        "merge_findings",
        {
            "sources": [
                [{"finding": "x", "criteria": ["security"], "location": "a.py:1"}],
                [
                    {"finding": "y", "criteria": ["tests"], "location": "a.py:1"}
                ],  # same loc, diff criterion
            ]
        },
    )
    assert out["clustered_count"] == 2  # different criteria don't cluster


# ── code_review_decide determinism (escalation can never change the verdict) ────────────────
def test_decide_is_a_pure_function_of_findings_and_verifs():
    findings = [{"id": "0", "finding": "x", "criteria": ["security"], "evidence": []}]
    out1 = _run_op("code_review_decide", {"findings": findings, "verifications": []})
    out2 = _run_op("code_review_decide", {"findings": findings, "verifications": []})
    assert out1["blocking"] == out2["blocking"]  # deterministic
    # nothing blocks in v1 (all criteria blocking_enabled=False) → no blocking findings
    assert out1["blocking"] == []


def test_verdict_steps_never_reference_the_escalation_steps():
    """The STRUCTURAL proof of 'escalation can NEVER change the verdict' (the AC): the
    decision-side steps (decide, verdict) must consume ONLY merge/verify/coach outputs — never
    `triggers`/`union` (which produce the overlay MEMBERSHIP). Escalation thus lives strictly on
    the recall/selection side; a flipped recommend_overlays can change WHICH overlays run but can
    never reach the Pass-3 decision. We assert this on the real gate YAML so a future edit that
    wires an escalation output into decide/verdict fails the build."""
    doc = yaml.safe_load(_GATE.read_text())
    by_id = {s["id"]: s for s in doc["steps"]}
    for sid in ("decide", "verdict"):
        refs = repr(by_id[sid].get("with") or {})
        assert "steps.triggers" not in refs, f"{sid} must not reference the triggers (Round-A) step"
        assert "steps.union" not in refs, f"{sid} must not reference the union (escalation) step"
    # and the decide op's inputs are exactly findings + verifications (no membership signal).
    assert set(by_id["decide"]["with"]) == {"findings", "verifications"}


def test_overlay_union_already_run_is_wired_to_the_round_a_triggers_output():
    """AC: the Round-B `union` step reads `already_run` from an explicit `with:` input wired to
    the Round-A `triggers` step's output (the one-hop bound) — NOT reconstructed from internal
    state. Asserted structurally on the real gate YAML."""
    doc = yaml.safe_load(_GATE.read_text())
    by_id = {s["id"]: s for s in doc["steps"]}
    union_with = by_id["union"]["with"]
    assert union_with["already_run"] == "${{ steps.triggers.outputs.to_run }}"
    # the triggers step IS the Round-A overlay_union call (recommend/already_run default empty).
    assert by_id["triggers"]["uses"] == "overlay_union"
    assert by_id["union"]["uses"] == "overlay_union"
    # union also takes the base reviewer's recommend + a configurable cap (the escalation inputs).
    assert union_with["recommend"] == "${{ steps.base.outputs.recommend_overlays }}"
    assert union_with["cap"] == 3


# ── the full gate workflow, OFFLINE ─────────────────────────────────────────────────────────
class _FakeRunner(AgentStepRunner):
    """Offline runner returning canned structured output per prompt; drives Round-B via the
    base reviewer's recommend_overlays."""

    def __init__(self, recommend=()):
        self.recommend = [{"overlay_id": o, "reason": "escalate"} for o in recommend]

    def run(self, ctx):
        prompt = ctx.step.get("prompt")
        schema = ctx.step.get("output_schema")
        if prompt == "code-review-base":
            return _ex.StepResult(
                outputs={
                    "findings": [
                        {
                            "finding": "base note",
                            "criteria": ["correctness"],
                            "evidence": ["a.py:1"],
                            "location": "a.py:1",
                        }
                    ],
                    "recommend_overlays": self.recommend,
                }
            )
        if schema == "code_review_findings":  # an overlay finder
            oid = str(prompt).replace("code-review-", "")
            return _ex.StepResult(
                outputs={
                    "findings": [
                        {
                            "finding": f"{oid} finding",
                            "criteria": [oid],
                            "evidence": [f"{oid}.py:1"],
                            "location": f"{oid}.py:1",
                        }
                    ]
                }
            )
        if schema == "verification":
            return _ex.StepResult(outputs={"verifications": []})  # no verifs → indeterminate → PASS
        if schema == "code_review_coach":
            return _ex.StepResult(outputs={"notes": []})
        return _ex.StepResult(outputs={"_fake": True})


class _Rec(_ex.RunRecorder):
    def __init__(self):
        self.store: dict = {}

    def run_started(self, record): ...
    def run_finished(self, record): ...

    def step_recorded(self, record):
        if record.get("status") == "running":
            return
        self.store[record.get("frame_key") or record.get("step_id")] = dict(record)

    def completed_step(self, run_id, frame_key):
        rec = self.store.get(frame_key)
        return rec if rec and rec.get("status") == "succeeded" else None


def _run_workflow(recommend, diff_text):
    doc = _migrate.migrate_to_current(yaml.safe_load(_GATE.read_text()))
    rec = _Rec()
    _ex.run_workflow(
        doc,
        # The caller provides ALL declared inputs (the engine errors on a referenced-but-unset
        # input); empty base/head/changed_files route to the diff_text path in assemble_diff.
        {"base": "HEAD~1", "head": "HEAD", "diff_text": diff_text, "changed_files": []},
        recorder=rec,
        scripted_registry=dict(_ex.STEP_REGISTRY),
        agent_runner=_FakeRunner(recommend=recommend),
        batch_runner=CodeReviewBatchRunner(context="## Diff\n(fake)"),
    )
    return rec


_DIFF = "diff --git a/src/auth/login.py b/src/auth/login.py\n+++ b/src/auth/login.py\n+x\n"


def test_workflow_runs_offline_round_b_tracks_escalation_verdict_invariant():
    # base escalates to `tests` + `performance`; the diff globs to `security` (Round-A).
    rec = _run_workflow(recommend=["tests", "performance"], diff_text=_DIFF)
    # Round-A: security glob-triggered (auth* path).
    assert "code-review-security" in rec.store["round_a"]["outputs"]["included"]
    # Round-B membership == the escalated set MINUS already-run (security ran in Round-A).
    rb_included = set(rec.store["round_b"]["outputs"]["included"])
    assert rb_included == {"code-review-tests", "code-review-performance"}
    assert "code-review-security" not in rb_included  # one-hop: not re-run in Round-B
    # the verdict is produced and is PASS (nothing blocks in v1).
    assert rec.store["verdict"]["outputs"]["verdict"] == "PASS"


def test_escalation_changes_membership_not_the_verdict():
    a = _run_workflow(recommend=["tests"], diff_text=_DIFF)
    b = _run_workflow(recommend=["docs", "performance"], diff_text=_DIFF)
    # different escalation → different Round-B membership ...
    assert set(a.store["round_b"]["outputs"]["included"]) == {"code-review-tests"}
    assert set(b.store["round_b"]["outputs"]["included"]) == {
        "code-review-docs",
        "code-review-performance",
    }
    # ... but the verdict is identical (escalation is recall-side only).
    assert (
        a.store["verdict"]["outputs"]["verdict"]
        == b.store["verdict"]["outputs"]["verdict"]
        == "PASS"
    )
