"""The OFFLINE scenario corpus for the plan-review planned-trace parity harness (B4).

A diverse set of stub-ticket fixtures — container vs leaf, overlays on/off, code-grounded,
oversize (P8), missing-AC (P1), cycle (P5), bug-exempt — each installed by monkeypatching
rebar's reads (``show_ticket`` / ``list_tickets``), exactly like the B1/B2 tests. No git
store, no LLM, no network.

Each :class:`Scenario` declares its ``kind``:

* ``parity`` — the precheck passes, BOTH paths run the four-pass LLM review, and their
  PRE-ESCALATION planned traces must be EQUAL (the safety net).
* ``block_divergent`` — a P1/P5 DET block. The WORKFLOW short-circuits with NO LLM call,
  whereas the bespoke ``run_review`` still runs the LLM pass before merging the DET block
  (the documented B2 divergence). The harness asserts EXACTLY that divergence.
* ``block_shared`` — a P8 too-big block or a bug-exempt type. BOTH paths skip the LLM, so
  their traces match (empty finder trace) — the block case that does NOT diverge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# A well-formed story body: AC checklist (P1 floor) + the per-type headings clarity rewards.
_GOOD_STORY = (
    "## Why\nThe system needs a durable X so downstream Y can rely on it.\n\n"
    "## What\nBuild X in `src/rebar/x.py`, wiring it through the existing seam.\n\n"
    "## Scope\nJust X; Z is out of scope.\n\n"
    "## Acceptance Criteria\n- [ ] X is observably persisted\n- [ ] the seam calls X\n"
)
_GOOD_TASK = (
    "## What\nImplement the helper in `src/rebar/h.py` behind the existing call site.\n\n"
    "## Acceptance Criteria\n- [ ] the helper returns the resolved value\n"
    "- [ ] the call site uses it\n"
)
_PERF_TASK = _GOOD_TASK + "\nWe must cut p99 latency on the hot path and add a cache.\n"
_EPIC_BODY = (
    "## Context\nThe X subsystem is being built out across a few coordinated tickets.\n\n"
    "## Success Criteria\nX ships end-to-end.\n\n"
    "## Acceptance Criteria\n- [ ] all children land\n- [ ] X works end-to-end\n"
)


@dataclass
class Scenario:
    name: str
    kind: str  # "parity" | "block_divergent" | "block_shared"
    ticket_id: str
    state: dict[str, Any]
    children: list[dict[str, Any]] = field(default_factory=list)
    extra_tickets: dict[str, dict[str, Any]] = field(default_factory=dict)
    window_tokens: int | None = None
    expected_verdict: str = "PASS"

    def install(self, monkeypatch) -> None:
        """Monkeypatch rebar's reads so both paths see this ticket graph (offline)."""
        import rebar

        table: dict[str, dict[str, Any]] = {
            self.ticket_id: self.state,
            **{c["ticket_id"]: c for c in self.children},
            **self.extra_tickets,
        }

        def _show(tid, repo_root=None):
            return dict(table[tid])

        def _list(parent=None, repo_root=None):
            return [dict(c) for c in self.children] if parent == self.ticket_id else []

        monkeypatch.setattr(rebar, "show_ticket", _show)
        monkeypatch.setattr(rebar, "list_tickets", _list)
        if self.window_tokens is not None:
            # P8 budget is derived from largest_window_tokens at assemble time; shrink it
            # so a modest oversize body trips P8 (both paths assemble via this name).
            from rebar.llm.plan_review import orchestrator

            monkeypatch.setattr(
                orchestrator, "largest_window_tokens", lambda *a, **k: self.window_tokens
            )


def _story(
    tid: str, description: str = _GOOD_STORY, *, ticket_type: str = "story", **kw
) -> dict[str, Any]:
    return {
        "ticket_id": tid,
        "ticket_type": ticket_type,
        "title": "Build X",
        "description": description,
        "deps": [],
        **kw,
    }


def _child(tid: str, *, deps=None, file_impact=None) -> dict[str, Any]:
    return {
        "ticket_id": tid,
        "ticket_type": "task",
        "title": f"child {tid}",
        "description": f"Implement {tid} in `src/rebar/{tid}.py`.\n\n"
        "## Acceptance Criteria\n- [ ] it works\n",
        "deps": deps or [],
        "file_impact": file_impact or [],
    }


def corpus() -> list[Scenario]:
    """The diverse offline scenario corpus."""
    return [
        # ── parity scenarios (precheck passes → full four-pass review on both paths) ──
        Scenario("leaf_story", "parity", "T-leaf", _story("T-leaf")),
        Scenario(
            "container_epic",
            "parity",
            "T-epic",
            {
                "ticket_id": "T-epic",
                "ticket_type": "epic",
                "title": "Epic X",
                "description": _EPIC_BODY,
                "deps": [],
            },
            children=[
                _child("ch1", file_impact=[{"path": "src/rebar/a.py", "reason": "a"}]),
                _child("ch2", file_impact=[{"path": "src/rebar/b.py", "reason": "b"}]),
            ],
        ),
        Scenario(
            "overlay_on",
            "parity",
            "T-perf",
            _story("T-perf", description=_PERF_TASK, ticket_type="task"),
        ),
        Scenario(
            "overlay_off",
            "parity",
            "T-clean",
            _story("T-clean", description=_GOOD_TASK, ticket_type="task"),
        ),
        Scenario(
            "code_grounded",
            "parity",
            "T-grounded",
            _story(
                "T-grounded",
                description=_GOOD_STORY
                + "\nGround against `src/rebar/store.py` and `src/rebar/api.py`.\n",
            ),
        ),
        Scenario(
            "isf_linked",
            "parity",
            "T-isf",
            _story("T-isf", deps=[{"target_id": "sl-1", "relation": "relates_to"}]),
            extra_tickets={
                "sl-1": {
                    "ticket_id": "sl-1",
                    "ticket_type": "session_log",
                    "title": "session: built X",
                    "description": "Decided X must persist to disk; dropped the in-memory variant.",
                    "deps": [],
                }
            },
        ),
        # ── block_divergent: P1 / P5 — workflow skips LLM, bespoke runs it ──
        Scenario(
            "missing_ac",
            "block_divergent",
            "T-noac",
            _story("T-noac", description="A body with no acceptance criteria block at all here."),
            expected_verdict="BLOCK",
        ),
        Scenario(
            "child_cycle",
            "block_divergent",
            "T-cycle",
            {
                "ticket_id": "T-cycle",
                "ticket_type": "epic",
                "title": "Epic cycle",
                "description": _EPIC_BODY,
                "deps": [],
            },
            children=[
                _child("cy1", deps=[{"target_id": "cy2", "relation": "depends_on"}]),
                _child("cy2", deps=[{"target_id": "cy1", "relation": "depends_on"}]),
            ],
            expected_verdict="BLOCK",
        ),
        # ── block_shared: P8 too-big + bug-exempt — both paths skip the LLM ──
        Scenario(
            "oversize_p8",
            "block_shared",
            "T-big",
            _story("T-big", description=_GOOD_STORY + ("\nfiller detail. " * 6000)),
            window_tokens=40_000,
            expected_verdict="BLOCK",
        ),
        Scenario(
            "bug_exempt",
            "block_shared",
            "T-bug",
            {
                "ticket_id": "T-bug",
                "ticket_type": "bug",
                "title": "A bug",
                "description": "## Reproduction Steps\n1. do X\n\n"
                "## Acceptance Criteria\n- [ ] fixed\n",
                "deps": [],
            },
            expected_verdict="PASS",
        ),
    ]
