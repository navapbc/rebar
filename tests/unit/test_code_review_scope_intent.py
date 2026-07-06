"""Story 3c5d (epic ac1b): the ``scope-intent`` overlay — the DIFF vs the UNION of the
scope/AC of the tickets named in the commit message's ``rebar-ticket:`` trailer(s).

Content-triggered (fires ONLY when >=1 trailer ticket resolves), ADVISORY, and — the hard
AC — TICKET-BLIND everywhere but this overlay: the ticket scope reaches ONLY scope-intent's
per-overlay ``context_override``; base + every other overlay keep the shared diff context.

All offline (no live LLM): the union-scope assembly is a structural read of the ticket store,
and the per-overlay injection is asserted against the batch runner directly.
"""

from __future__ import annotations

import pathlib
import subprocess

import pytest

import rebar
from rebar.llm.code_review import assemble as A
from rebar.llm.code_review import registry as reg
from rebar.llm.code_review.batch_runner import CodeReviewBatchRunner
from rebar.llm.workflow.runners import BatchRunRequest

pytestmark = pytest.mark.unit

_DIFF = (
    "diff --git a/src/rebar/foo.py b/src/rebar/foo.py\n"
    "--- a/src/rebar/foo.py\n+++ b/src/rebar/foo.py\n"
    "@@ -1,2 +1,2 @@\n-old = 1\n+new = 2\n"
)


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A real ticket store with two tickets carrying distinctive scope/AC prose so the union
    assembly can be asserted structurally (both tickets' scope must appear, no drift by
    construction)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@e.com"), ("config", "user.name", "T")):
        subprocess.run(["git", *args], cwd=repo, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    a = rebar.create_ticket(
        "task",
        "Widget parser",
        description=(
            "## What\nParse the WIDGET grammar.\n\n## Scope\n- ONLY the parser module.\n\n"
            "## Acceptance Criteria\n- [ ] SCOPEALPHA parser handles nested widgets.\n"
        ),
        repo_root=str(repo),
    )
    b = rebar.create_ticket(
        "task",
        "Widget renderer",
        description=(
            "## What\nRender parsed widgets.\n\n## Scope\n- ONLY the renderer module.\n\n"
            "## Acceptance Criteria\n- [ ] SCOPEBETA renderer emits valid HTML.\n"
        ),
        repo_root=str(repo),
    )
    return {"repo": str(repo), "a": a, "b": b}


# ── the sync invariant: scope-intent ∈ OVERLAY_IDS ∧ has a routing entry, advisory ─────────
def test_scope_intent_is_a_registered_advisory_overlay_with_routing():
    assert "scope-intent" in reg.OVERLAY_IDS
    idx = reg.routing_index()
    assert "scope-intent" in idx, "scope-intent overlay has no criteria_routing.json entry"
    entry = idx["scope-intent"]
    assert entry["exec"] in ("AGENT", "1-TURN")
    assert entry["applies_to"] == []  # content-triggered (trailer-driven), not glob
    assert entry["blocking_enabled"] is False  # ADVISORY — no new BLOCK source
    assert reg.threshold_for(["scope-intent"]) == (0.95, False)


def test_scope_intent_flag_key_is_underscored():
    assert reg.overlay_flag_key("scope-intent") == "include_scope_intent"


# ── assemble_diff_context: union scope-context ONLY when a trailer ticket resolves ─────────
def test_union_scope_context_from_resolving_trailer(store):
    msg = f"feat: parse widgets\n\nrebar-ticket: {store['a']}\n"
    dc = A.assemble_diff_context(diff_text=_DIFF, commit_message=msg, repo_root=store["repo"])
    assert dc.scope_context, "a resolving trailer must produce a non-empty union scope-context"
    assert "SCOPEALPHA" in dc.scope_context  # the referenced ticket's AC is present


def test_no_scope_context_without_trailer_or_when_unresolvable(store):
    # no trailer at all → overlay inert
    dc = A.assemble_diff_context(diff_text=_DIFF, commit_message="", repo_root=store["repo"])
    assert dc.scope_context == ""
    # a trailer that resolves to NOTHING in the store → still inert
    dc2 = A.assemble_diff_context(
        diff_text=_DIFF,
        commit_message="feat: x\n\nrebar-ticket: no-such-ticket\n",
        repo_root=store["repo"],
    )
    assert dc2.scope_context == ""


def test_base_and_diff_context_stay_ticket_blind(store):
    # The HARD AC: the shared diff `context` (what base + every other overlay see) must NEVER
    # carry the ticket scope — only dc.scope_context does.
    msg = f"feat: x\n\nrebar-ticket: {store['a']}\n"
    dc = A.assemble_diff_context(diff_text=_DIFF, commit_message=msg, repo_root=store["repo"])
    assert "SCOPEALPHA" not in dc.context
    assert "SCOPEALPHA" in dc.scope_context  # it lives ONLY here


def test_multi_ticket_commit_union_names_all_tickets(store):
    # A legitimate multi-ticket commit naming ALL its tickets: the union must contain EVERY
    # referenced ticket's scope, so a faithful multi-ticket change reads as in-scope (no false
    # drift) — asserted structurally on the union, no live LLM.
    msg = f"feat: widget pipeline\n\nrebar-ticket: {store['a']} {store['b']}\n"
    dc = A.assemble_diff_context(diff_text=_DIFF, commit_message=msg, repo_root=store["repo"])
    assert "SCOPEALPHA" in dc.scope_context and "SCOPEBETA" in dc.scope_context


# ── CodeReviewBatchRunner: per-overlay context_overrides, shared diff for the rest ─────────
class _RecordingAgentRunner:
    """Records the ``ticket_context`` each overlay was invoked with."""

    def __init__(self):
        self.seen: dict[str, str] = {}

    def run(self, ctx):
        self.seen[ctx.step["prompt"]] = ctx.inputs.get("ticket_context")

        class _R:
            outputs = {"findings": []}

        return _R()


def _req(*prompts):
    return BatchRunRequest(
        finder="code-review-base",
        criteria=tuple({"prompt": p} for p in prompts),
        usd_budget=None,
        model_ladder=(),
        workflow={},
        target_ticket=None,
        repo_root=None,
        run_id="r",
        step_id="s",
    )


def test_batch_runner_injects_override_for_scope_intent_and_shared_diff_for_others():
    ar = _RecordingAgentRunner()
    runner = CodeReviewBatchRunner(
        context="DIFF_CONTEXT",
        context_overrides={"code-review-scope-intent": "UNION_TICKET_SCOPE"},
    )
    runner.run(_req("code-review-scope-intent", "code-review-security"), ar)
    assert ar.seen["code-review-scope-intent"] == "UNION_TICKET_SCOPE"  # its own ticket context
    assert ar.seen["code-review-security"] == "DIFF_CONTEXT"  # ticket-blind: shared diff only


def test_batch_runner_without_overrides_gives_every_overlay_the_shared_diff():
    ar = _RecordingAgentRunner()
    runner = CodeReviewBatchRunner(context="DIFF_CONTEXT")  # default None overrides
    runner.run(_req("code-review-scope-intent", "code-review-security"), ar)
    assert ar.seen["code-review-scope-intent"] == "DIFF_CONTEXT"
    assert ar.seen["code-review-security"] == "DIFF_CONTEXT"


# ── the prompt loads with the overlay contract and is a canonical fixed point ──────────────
def test_scope_intent_prompt_resolves_as_a_code_review_pass_finder():
    from rebar.llm.prompting.prompts import get_prompt

    p = get_prompt("code-review-scope-intent")
    assert p.outputs == "code_review_findings"
    assert p.category == "code-review-pass"
    assert not p.is_reviewer


def test_scope_intent_prompt_is_canonical_front_matter_fixed_point():
    from rebar.llm.prompting.prompts_frontmatter import _split_front_matter_raw, write_front_matter

    path = pathlib.Path("src/rebar/llm/reviewers/code-review-scope-intent.md")
    text = path.read_text(encoding="utf-8")
    assert write_front_matter(*_split_front_matter_raw(text)) == text
