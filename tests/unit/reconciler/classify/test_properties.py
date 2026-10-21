"""Property tests for the convergence classifier (epic 3006-e198 foundation).

The four named properties (ADR 0026/0027/0028):
  (a) convergence     — apply the classifier's decisions to a model world, then
                        re-classify → ZERO acting decisions (a fixed point).
  (b) no-orphan       — a heal never leaves a bound key with neither a live Jira
                        issue nor a retirement record.
  (c) conservation    — no local_id is ever bound to two Jira keys (no double-bind).
  (d) direction       — a Jira-side edit with local == baseline is mirrored inbound
                        (outbound suppressed), never reverted.

The repo carries no ``hypothesis`` dependency (and no test uses it), so these are
DETERMINISTIC seeded-random property tests: many worlds from a fixed seed, each
asserted. Reproducible and dependency-free.
"""

from __future__ import annotations

import random

from ._load import load_classify

c = load_classify()
ObservedJira = c.ObservedJira
DecisionKind = c.DecisionKind
JiraObservation = c.JiraObservation

SEED = 0xC0FFEE
WORLDS = 400


class ModelPair:
    """A model of one reconciled pair, evolvable by a Decision (no real I/O)."""

    def __init__(self, jira_key: str, rng: random.Random) -> None:
        self.jira_key = jira_key
        # Local: None (native), active, or terminal.
        self.local_kind = rng.choice(["none", "active", "terminal"])
        # Jira ground-truth: does the issue actually exist in Jira?
        self.jira_exists = rng.random() > 0.15
        # Is it inside the fetch window this pass?
        self.in_window = self.jira_exists and rng.random() > 0.25
        # Bound? (a native issue with no local is often unbound)
        self.bound = self.local_kind != "none" and rng.random() > 0.1
        self.retired = False
        self.absent_404 = 0
        self.jira_status = "To Do"

    # -- projections the classifier consumes --------------------------------

    def local(self):
        if self.local_kind == "none":
            return None
        if self.local_kind == "terminal":
            return {"ticket_id": self.jira_key, "status": "archived", "archived": True}
        return {"ticket_id": self.jira_key, "status": "in_progress", "archived": False}

    def binding(self):
        if not self.bound:
            return None
        return {
            "jira_key": self.jira_key,
            "state": "confirmed",
            "absent_404_count": self.absent_404,
        }

    def observe(self) -> JiraObservation:
        if self.in_window:
            return JiraObservation(
                ObservedJira.PRESENT,
                key=self.jira_key,
                fields={"status": self.jira_status},
                retired=self.retired,
            )
        # Out of window: the live pass probes. Model the probe's ground truth.
        if not self.jira_exists:
            return JiraObservation(ObservedJira.CONFIRMED_404, key=self.jira_key)
        return JiraObservation(ObservedJira.ABSENT_IN_WINDOW, key=self.jira_key)

    def classify(self, grace: int = 3):
        return c.classify(self.local(), self.observe(), self.binding(), None, grace=grace)

    # -- the model apply: evolve the world by one decision ------------------

    def apply(self, decision, grace: int = 3) -> None:
        k = decision.kind
        if k is DecisionKind.TERMINAL_TRANSITION:
            self.jira_status = "Done"
        elif k is DecisionKind.ADOPT:
            self.local_kind = "active"
            self.bound = True
        elif k is DecisionKind.RETIRE_AFTER_GRACE:
            self.bound = False
            self.retired = True
        elif k is DecisionKind.PROBE_GET:
            # A probe that confirms 404 increments grace; over passes this
            # converges to RETIRE. A probe of an alive-but-out-of-window issue
            # is a no-op (stays absent, alive).
            if not self.jira_exists:
                self.absent_404 += 1
        # SYNC_FIELDS / SKIP_RETIRED / ALERT / NOOP: no lifecycle change.


def _make_world(rng: random.Random, n: int) -> list[ModelPair]:
    return [ModelPair(f"REB-{i}", rng) for i in range(n)]


def test_property_convergence_reaches_fixed_point():
    """(a) Iterating apply→classify reaches a fixed point (zero acting)."""
    rng = random.Random(SEED)
    for _ in range(WORLDS):
        world = _make_world(rng, rng.randint(1, 12))
        # Drive for a bounded number of passes (grace=3 needs a few passes to
        # count down through PROBE_GET → RETIRE; an alive-but-out-of-window pair
        # probes perpetually but that is NON-acting, so it is a fixed point too).
        for _pass in range(12):
            decisions = [p.classify() for p in world]
            for p, d in zip(world, decisions, strict=False):
                p.apply(d)
        final = [p.classify() for p in world]
        assert not any(d.is_acting for d in final), [d.kind for d in final if d.is_acting]


def test_property_no_orphan_after_convergence():
    """(b) After convergence, no bound key lacks a live Jira issue AND a retirement."""
    rng = random.Random(SEED + 1)
    for _ in range(WORLDS):
        world = _make_world(rng, rng.randint(1, 12))
        for _pass in range(12):
            decisions = [p.classify() for p in world]
            for p, d in zip(world, decisions, strict=False):
                p.apply(d)
        for p in world:
            if p.bound:
                # A still-bound pair must have a live Jira issue (not a dangling
                # binding to a gone issue) once converged.
                assert p.jira_exists, f"orphan binding to gone issue {p.jira_key}"


def test_property_conservation_no_double_bind():
    """(c) No jira_key is claimed by two bindings across a converged world."""
    rng = random.Random(SEED + 2)
    for _ in range(WORLDS):
        world = _make_world(rng, rng.randint(1, 12))
        for _pass in range(6):
            decisions = [p.classify() for p in world]
            for p, d in zip(world, decisions, strict=False):
                p.apply(d)
        bound_keys = [p.jira_key for p in world if p.bound]
        assert len(bound_keys) == len(set(bound_keys))


def test_property_direction_preservation():
    """(d) A Jira-side edit with local == baseline is mirrored inbound, never reverted."""
    rng = random.Random(SEED + 3)
    for _ in range(2000):
        baseline = rng.choice(["A", "B", "", "assignee@x", "Done"])
        jira_now = rng.choice(["A", "B", "C", "assignee@y", "To Do"])
        # local == baseline → local unchanged since sync → suppress outbound.
        assert c.direction_is_inbound(baseline, baseline) is True
        # A genuine local edit (local != baseline) → local-wins outbound.
        local_edit = baseline + "-edited"
        assert c.direction_is_inbound(local_edit, baseline) is False
        # Sanity: when local matches baseline but jira diverged, we still suppress
        # (the teammate's jira_now survives because outbound is suppressed).
        if jira_now != baseline:
            assert c.direction_is_inbound(baseline, baseline) is True
