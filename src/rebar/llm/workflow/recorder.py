"""Run-state recorder seam for the workflow executor (WS-C3).

Extracted from ``executor.py`` (it sat exactly at the module-size cap) along the
natural call-graph seam: the recorder abstraction is self-contained — the base
``RunRecorder``, the in-memory default, and the durable event-backed recorder —
and depends only on the leaf-write seam (lazily). ``executor`` re-exports these
names, so every existing ``executor.MemoryRecorder`` / ``TicketEventRecorder``
reference keeps working.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class RunRecorder:
    """Persistence seam for run-state. The default is in-memory; WS-C3 adds the
    WORKFLOW_RUN/WORKFLOW_STEP event-backed recorder with marker-after-effect
    idempotency."""

    def run_started(self, record: dict[str, Any]) -> None: ...
    def run_finished(self, record: dict[str, Any]) -> None: ...
    def step_recorded(self, record: dict[str, Any]) -> None: ...

    def completed_step(self, run_id: str, frame_key: str) -> dict[str, Any] | None:
        """Return a prior SUCCEEDED record for this FRAME KEY (idempotent skip), or
        None. ``frame_key`` is the bare ``step_id`` at the top frame or an
        iteration-embedding path inside a loop/map body.

        The in-memory default never skips (no prior runs); WS-C3's event recorder
        returns the persisted marker so a resumed run does not re-run a committed
        effect.
        """
        return None


class MemoryRecorder(RunRecorder):
    """Collects run/step records in memory (default; keeps the executor testable)."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []

    def run_started(self, record: dict[str, Any]) -> None:
        self.runs.append(record)

    def run_finished(self, record: dict[str, Any]) -> None:
        self.runs.append(record)

    def step_recorded(self, record: dict[str, Any]) -> None:
        self.steps.append(record)


class TicketEventRecorder(RunRecorder):
    """Durable run-state on the target ticket's event log (WS-C3).

    Each call appends a WORKFLOW_RUN/WORKFLOW_STEP event (per-key LWW, WS-C1) to the
    target ticket. The executor calls ``step_recorded`` AFTER a step's effect
    commits — the marker-after-effect rule: a crash between effect and marker leaves
    the effect *applied but unmarked*, so forward-only recovery re-runs the step,
    which is safe because side-effecting steps are idempotent on (run_id, step_id).
    ``completed_step`` reads the persisted marker so a resumed run skips steps that
    DID get marked. All store imports are lazy so the module stays import-light.
    """

    def __init__(self, target_ticket: str, repo_root: str | None = None) -> None:
        self.ticket = target_ticket
        self.repo_root = repo_root
        self._validated = False

    def _append(self, event_type: str, data: dict[str, Any]) -> None:
        from rebar._commands import _seam

        tracker = _seam.tracker_dir(self.repo_root)
        # Resolve + ghost-check the target ONCE, before the first event is written
        # (bug bind-hcd-dam). The leaf-write commands guard with require_id +
        # require_not_ghost; the recorder did neither, so a bogus/ghost id flowed
        # straight into append_event whose committer does makedirs(exist_ok=True),
        # materializing a phantom CREATE-less directory that fsck flags forever.
        # Doing it here — the single chokepoint for the library/CLI/MCP entry points
        # — fails fast (no event file written) and resolves an alias to its canonical
        # dir so run-state never lands on a `<alias>/` phantom.
        if not self._validated:
            resolved = _seam.require_id(self.ticket, tracker)
            _seam.require_not_ghost(resolved, tracker)
            self.ticket = resolved
            self._validated = True
        _seam.append_event(self.ticket, event_type, data, tracker, repo_root=self.repo_root)

    def run_started(self, record: dict[str, Any]) -> None:
        self._append("WORKFLOW_RUN", record)

    def run_finished(self, record: dict[str, Any]) -> None:
        self._append("WORKFLOW_RUN", record)

    def step_recorded(self, record: dict[str, Any]) -> None:
        self._append("WORKFLOW_STEP", record)

    def completed_step(self, run_id: str, frame_key: str) -> dict[str, Any] | None:
        from rebar._commands import _seam
        from rebar.reducer import reduce_ticket

        tracker = _seam.tracker_dir(self.repo_root)
        try:
            state = reduce_ticket(str(Path(tracker) / self.ticket))
        except Exception:  # noqa: BLE001 — reduce_ticket fallback: an unreducible ticket yields no state (None)
            return None
        if not state:
            return None
        return state.get("workflow_steps", {}).get(run_id, {}).get(frame_key)
