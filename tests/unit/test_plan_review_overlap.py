"""Store-derived overlap context for plan review (a350-2e7e-71eb-4f5b, epic 62dc).

Two tickets planning one change both pass review today, because each plan is read in
isolation. This module exercises the retrieval that fixes that, and — more of the surface
than the feature itself — the guarantees that make it safe to add: the off switch is
total, the finding can never block, and a degraded retrieval is distinguishable from an
honest empty match.

Stores are built by writing event files directly, the way
``tests/unit/test_similar_tickets_index.py`` does: the reducer folds a plain event
directory, so no git repository is needed and these stay in the unit tier.

Nothing here asserts that a model does or does not score a plan as overlapping. Such a
test could only assert a canned score, which proves the fixture rather than the criterion
— the same reasoning ``test_project_net_mechanism_criterion.py`` records. What is proven
here is the plumbing on both sides of the model: that a real seeded candidate reaches the
prompt, and that a finding attributed to the criterion lands advisory.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

from rebar.llm.criteria.model import threshold_for
from rebar.llm.plan_review import orchestrator, overlap, registry
from rebar.llm.plan_review.checkpoints import checkpoint_identity
from rebar.llm.plan_review.context_assembly import assemble_context
from rebar.llm.plan_review.pass1 import _chunk_extra_context
from rebar.llm.plan_review.production_batch_runner import _OVERLAP_ID, ProductionBatchRunner
from rebar.llm.prompting import prompt_library
from rebar.llm.review_kernel import GRADED_BINARY
from rebar.llm.runner import FakeRunner
from rebar.llm.workflow.runners import BatchRunRequest
from rebar.similar import RESULT_LIMIT, find_similar

pytestmark = pytest.mark.unit

_TS = 1793585542519320001
_TARGET = "abcd-0000-0000-0001"
# The suite points ``REBAR_ROOT`` at a sandbox repo, so the project overlay that activates
# this criterion is only reachable by naming the real checkout, exactly as
# ``test_project_net_mechanism_criterion.py`` does.
REPO_ROOT = str(Path(__file__).resolve().parents[2])


# The criterion's own rubric QUOTES the heading, so the heading alone cannot distinguish an
# injected candidate block from the rubric that describes one. The directive line is emitted
# only by the renderer, so it is the marker that actually discriminates.
_INJECTED_MARKER = overlap._DIRECTIVE


def _routing() -> dict:
    prompt_library._invalidate_caches()
    return registry.effective_routing(REPO_ROOT)[_OVERLAP_ID]


# The plan under review and the seeded plan that restates it. They share the vocabulary a
# lexical index ranks on, which is what makes the seeded candidate retrievable without a
# model provider.
_SHARED = (
    "rename the plan-review checkpoint identity digest so the resume key binds the "
    "injected store context, and record the retrieval status in coverage"
)
_PLAN = (
    "## Why\nresume keys collide.\n\n## What\n" + _SHARED + " in `src/rebar/x.py`.\n\n"
    "## Scope\njust that.\n\n## Acceptance Criteria\n"
    "- [ ] the digest binds the context\n- [ ] the status is recorded\n"
)


def _event(ticket_dir: Path, event_type: str, data: dict) -> None:
    event_uuid = str(uuid.uuid4())
    payload = {
        "uuid": event_uuid,
        "env_id": "test-env",
        "timestamp": _TS,
        "event_type": event_type,
        "author": "t",
        "author_email": "t@e.com",
        "data": data,
    }
    (ticket_dir / f"{_TS}-{event_uuid}-{event_type}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _seed(tracker: Path, count: int) -> list[str]:
    """Seed *count* tickets whose descriptions restate ``_SHARED``. Returns their aliases."""
    aliases = []
    for i in range(count):
        tid = f"beef-0000-0000-{i:04d}"
        ticket_dir = tracker / tid
        ticket_dir.mkdir(parents=True)
        alias = f"seeded-plan-{i}"
        _event(
            ticket_dir,
            "CREATE",
            {
                "id": tid,
                "alias": alias,
                "title": "Bind the checkpoint identity digest",
                "description": _SHARED + f" (variant {i})",
                "ticket_type": "task",
                "priority": 2,
                "tags": [],
                "parent_id": "",
                "creation_channel": "python",
            },
        )
        aliases.append(alias)
    return aliases


@pytest.fixture
def tracker(tmp_path: Path) -> Path:
    path = tmp_path / "store" / ".tickets-tracker"
    path.mkdir(parents=True)
    return path


@pytest.fixture
def plan_reads(monkeypatch, tracker: Path, request):
    """Reconstruct a synthetic target ticket with no children, rooted at the seeded store.

    The plan carries a per-test nonce. Pass-1 checkpoints are keyed by an identity over the
    material, and the suite shares one checkpoint store, so two tests reviewing byte-identical
    plans would have the second RESUME the first's result and never reach the runner — which
    would silently void every call-count and injected-prompt assertion below.
    """
    state = {
        "ticket_id": _TARGET,
        "ticket_type": "story",
        "title": "Bind the checkpoint identity digest",
        "description": _PLAN + f"\n<!-- {request.node.name} {uuid.uuid4().hex} -->\n",
        "deps": [],
    }
    monkeypatch.setattr("rebar._reads.show_ticket", lambda tid, *, repo_root=None: dict(state))
    monkeypatch.setattr("rebar._reads.list_tickets", lambda *, parent=None, repo_root=None: [])
    monkeypatch.setattr(
        "rebar.llm.plan_review.context_assembly.current_tickets_root", lambda: str(tracker)
    )
    return state


class _Recorder(FakeRunner):
    """A ``FakeRunner`` that counts its calls and keeps the instructions it was given.

    Counting is the only way to prove the AC's "no LLM call is added" claim, and the
    instructions are where the injected overlap block becomes observable.
    """

    def __init__(self, *, findings_for: tuple[str, ...] = ()) -> None:
        super().__init__(
            structured={
                "analysis": "",
                "findings": [{"finding": f"f-{cid}", "criteria": [cid]} for cid in findings_for],
            }
        )
        self.calls = 0
        self.instructions: list[str] = []

    def run(self, req):  # type: ignore[override]
        self.calls += 1
        self.instructions.append(req.instructions)
        return super().run(req)


def _request(*, overlap_on: bool, repo_root: str | None = REPO_ROOT) -> BatchRunRequest:
    return BatchRunRequest(
        finder="plan-review-finder",
        criteria=({"prompt": "E2"},),
        usd_budget=None,
        model_ladder=("claude-opus-4-8",),
        workflow={},
        target_ticket=_TARGET,
        repo_root=repo_root,
        run_id="run-1",
        step_id="finders",
        # The probe allowlist is the production seam that narrows the project fan-in. Using
        # it keeps these runs to the one criterion under test: without it the real overlay
        # also fans in the AGENT-tier project criteria, whose tool loop needs a git-backed
        # store these event-directory fixtures deliberately do not build.
        with_inputs={"overlap": overlap_on, "probe_criteria": [_OVERLAP_ID]},
    )


def _run(runner: _Recorder, *, overlap_on: bool) -> dict:
    """Run one batch review and return its ``coverage`` plan plus findings."""
    result = ProductionBatchRunner(runner=runner).run(_request(overlap_on=overlap_on), None)
    return {
        "coverage": result.outputs["batch_plan"],
        "findings": result.outputs["findings"],
    }


# ── AC1: a seeded candidate reaches the criterion, and the cap holds ───────────


def test_seeded_candidate_reaches_the_prompt_and_lands_advisory(plan_reads, tracker):
    _seed(tracker, 1)
    runner = _Recorder(findings_for=(_OVERLAP_ID,))
    out = _run(runner, overlap_on=True)

    injected = "\n".join(runner.instructions)
    assert _INJECTED_MARKER in injected
    assert "seeded-plan-0" in injected, "the seeded ticket must be named in the prompt"
    assert out["coverage"]["overlap"]["status"] == "ok"

    attributed = [f for f in out["findings"] if _OVERLAP_ID in (f.get("criteria") or [])]
    assert attributed, "a finding attributed to the overlap criterion must survive the run"
    assert _routing()["default_posture"] == "advisory"


def test_more_than_five_matches_inject_exactly_five(plan_reads, tracker):
    _seed(tracker, 8)
    runner = _Recorder()
    out = _run(runner, overlap_on=True)

    assert out["coverage"]["overlap"]["candidates"] == RESULT_LIMIT == 5
    injected = "\n".join(runner.instructions)
    named = [a for a in (f"seeded-plan-{i}" for i in range(8)) if a in injected]
    assert len(named) == 5, f"expected exactly 5 candidates in the prompt, saw {named}"


# ── AC2: the off switch is total ──────────────────────────────────────────────


def test_off_emits_no_overlap_finding(plan_reads, tracker):
    _seed(tracker, 3)
    runner = _Recorder(findings_for=(_OVERLAP_ID,))
    out = _run(runner, overlap_on=False)

    assert not [f for f in out["findings"] if _OVERLAP_ID in (f.get("criteria") or [])]
    assert _INJECTED_MARKER not in "\n".join(runner.instructions)


def test_off_keeps_the_criterion_out_of_batch_resolution(plan_reads, tracker):
    _seed(tracker, 3)
    off = _run(_Recorder(), overlap_on=False)
    on = _run(_Recorder(), overlap_on=True)

    assert _OVERLAP_ID not in off["coverage"]["batch_resolution"]["single"]
    assert _OVERLAP_ID in on["coverage"]["batch_resolution"]["single"]


def test_off_leaves_checkpoint_identity_byte_identical(plan_reads, tracker):
    """The pre-change call had no ``overlap_context`` argument at all. With the switch off
    the new argument is ``""``, so the extra context — and the digest over it — must be the
    same bytes even for a chunk that contains the overlap criterion."""
    _seed(tracker, 3)
    ctx = assemble_context(_TARGET, repo_root=REPO_ROOT)
    chunk = [{"id": _OVERLAP_ID}]
    before = _chunk_extra_context(chunk, ctx, "")
    after = _chunk_extra_context(chunk, ctx, "", "")
    assert before == after

    identity = dict(material=ctx.plan_text, chunk=chunk, model="m", agentic=False)
    assert checkpoint_identity(**identity, extra_context=before) == checkpoint_identity(
        **identity, extra_context=after
    )


def test_on_adds_no_llm_call(plan_reads, tracker):
    """The candidates ride inside a chunk that is already being sent."""
    _seed(tracker, 3)
    off, on = _Recorder(), _Recorder()
    _run(off, overlap_on=False)
    _run(on, overlap_on=True)

    assert off.calls > 0
    assert on.calls == off.calls


# ── AC3: an overlap finding can never turn a pass into a block ────────────────


@pytest.fixture
def overlay_registry(monkeypatch):
    """Make the orchestrator's ambient ``registry.by_id()`` see the project overlay.

    ``pass3_over_findings`` and ``_any_blocking_criterion`` both read the registry with no
    repo root, which under the suite's sandboxed ``REBAR_ROOT`` omits every project
    criterion — an unknown id would then fall through to a default posture and the
    assertions below would pass without the committed routing entry existing at all.
    """
    prompt_library._invalidate_caches()
    view = registry.by_id(REPO_ROOT)
    assert _OVERLAP_ID in view
    monkeypatch.setattr(registry, "by_id", lambda *a, **k: view)
    return view


def test_routing_posture_is_advisory(overlay_registry):
    routing = _routing()
    assert routing["default_posture"] == "advisory"
    assert registry.exec_tier(overlay_registry[_OVERLAP_ID]) != "AGENT"
    assert threshold_for([_OVERLAP_ID], overlay_registry, gate="plan_review")[1] is False


def test_decide_pass_places_the_finding_in_advisory(monkeypatch, overlay_registry):
    """Even at maximum impact the decide pass must not reach ``block``. Pinning impact at
    0.97 is what makes this a real test: the same inputs on a blocking criterion DO block
    (``test_evidence_kind_criterion``), so a decision of ``advisory`` here can only come
    from the criterion's posture."""
    monkeypatch.setattr(orchestrator.review_kernel, "impact_plan", lambda attrs: 0.97)
    finding = {"criteria": [_OVERLAP_ID], "finding": "This plan restates seeded-plan-0."}
    verifications = {0: {"binary": dict.fromkeys(GRADED_BINARY, "yes"), "severity_attributes": {}}}

    decided = orchestrator.pass3_over_findings([finding], verifications)[0]
    assert decided["blocking_enabled"] is False
    assert decided["decision"] != "block"

    parts = orchestrator.partition_findings([], [], [decided])
    assert not parts["blocking"]
    assert [f for f in parts["surfaced"] if _OVERLAP_ID in (f.get("criteria") or [])]


def test_a_passing_plan_stays_passing_with_an_overlap_finding(monkeypatch, overlay_registry):
    monkeypatch.setattr(orchestrator.review_kernel, "impact_plan", lambda attrs: 0.97)
    finding = {"criteria": [_OVERLAP_ID], "finding": "This plan restates seeded-plan-0."}
    verifications = {0: {"binary": dict.fromkeys(GRADED_BINARY, "yes"), "severity_attributes": {}}}
    decided = orchestrator.pass3_over_findings([finding], verifications)

    clean = orchestrator.partition_findings([], [], [])
    flagged = orchestrator.partition_findings([], [], decided)
    assert clean["blocking"] == flagged["blocking"] == []
    # The verdict cannot degrade either: the fail-closed path keys on whether ANY finding
    # maps to a blocking-posture criterion, and an overlap finding never does.
    assert orchestrator._any_blocking_criterion(decided) is False


# ── AC4: compatibility ────────────────────────────────────────────────────────


def test_find_similar_without_diagnostics_is_unchanged(tracker):
    """The ``recent_creates.py`` call passes no ``diagnostics``; it must behave as before."""
    _seed(tracker, 3)
    with_diag: dict[str, Any] = {}
    assert find_similar(str(tracker), text=_SHARED, diagnostics=with_diag) == find_similar(
        str(tracker), text=_SHARED
    )
    assert with_diag["status"] == "ok"


def test_lexical_tier_serves_with_no_model_provider(monkeypatch, tracker):
    """The embedding tier is opt-in; unconfigured, retrieval still returns candidates."""
    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "REBAR_EMBEDDING_MODEL"):
        monkeypatch.delenv(var, raising=False)
    _seed(tracker, 3)
    diag: dict[str, Any] = {}
    results = find_similar(str(tracker), text=_SHARED, diagnostics=diag)
    assert diag["status"] == "ok"
    assert results, "the lexical tier is the floor and must serve without a provider"


# ── AC5: degradation is recorded, empty, and verdict-neutral ──────────────────


@pytest.mark.parametrize(
    ("status", "break_it"),
    [
        ("index-absent", "absent"),
        ("catch-up-failed", "catchup"),
        ("retrieval-error", "retrieval"),
    ],
)
def test_degraded_retrieval_records_its_status_and_changes_nothing(
    plan_reads, tracker, status, break_it
):
    _seed(tracker, 3)

    def _raise(*a, **k):
        raise RuntimeError("induced")

    # A nested context, not the test's own ``monkeypatch``: undoing the latter would also
    # revert the ``plan_reads`` stubs this test still needs for the comparison run below.
    with pytest.MonkeyPatch.context() as broken:
        if break_it == "absent":
            broken.setattr("rebar.similar.open_index", lambda *a, **k: None)
        elif break_it == "catchup":
            broken.setattr("rebar.similar.catch_up", _raise)
        else:
            broken.setattr("rebar.similar.search", _raise)
            broken.setattr("rebar.similar.embedding_rank", lambda *a, **k: None)

        degraded_runner = _Recorder()
        degraded = _run(degraded_runner, overlap_on=True)

    assert degraded["coverage"]["overlap"]["status"] == status
    assert degraded["coverage"]["overlap"]["candidates"] == 0
    assert _INJECTED_MARKER not in "\n".join(degraded_runner.instructions)
    assert not [f for f in degraded["findings"] if _OVERLAP_ID in (f.get("criteria") or [])]

    off = _run(_Recorder(), overlap_on=False)
    assert degraded["findings"] == off["findings"]


# ── AC6: the tree-wide gates ──────────────────────────────────────────────────


def test_added_and_modified_modules_are_within_the_size_cap():
    repo = Path(__file__).resolve().parents[2]
    limit = int((repo / ".github" / "module-size-limit.txt").read_text().strip())
    touched = [
        "llm/plan_review/overlap.py",
        "llm/plan_review/pass1.py",
        "llm/plan_review/production_batch_runner.py",
        "llm/plan_review/__init__.py",
        "llm/workflow/gate_dispatch.py",
        "similar/__init__.py",
    ]
    for rel in touched:
        path = repo / "src" / "rebar" / rel
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines <= limit, f"{rel} is {lines} lines, over the {limit} cap"


@pytest.mark.parametrize("script", ["check_mechanism_delta.py", "check_complexity_baseline.py"])
def test_ratchets_exit_zero(script):
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run(
        [sys.executable, str(repo / "scripts" / script), "--check"],
        capture_output=True,
        text=True,
        cwd=repo,
        check=False,
    )
    assert proc.returncode == 0, f"{script} failed:\n{proc.stdout}\n{proc.stderr}"
