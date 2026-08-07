"""Confirmation matrix: is the completion-verifier runaway budget-anchored?

diag_runaway_transcript.py showed a SINGLE f6fc verification converge (PASS, 98 calls)
at a 50-step floor after the same tree ran away (196 calls) at the 480-step floor. n=1
each is not causal. This driver runs f6fc verification K times at each of several step
floors and records, per run: total tool calls (transcript proxy), whether the primary
run tripped the in-flight runaway guard, and the returned verdict. If the runaway is
budget-anchored, tool-call volume and the trip rate should RISE with the floor.

Local-only, opt-in; raw transcripts kept out of the repo. Results -> /tmp/diag_matrix.csv.

    AWS_REGION=us-east-1 REBAR_LLM_STANDARD_PROVIDER=bedrock \
    REBAR_LLM_STANDARD_MODEL=us.anthropic.claude-sonnet-4-6 REBAR_GATE_ALLOW_UNGATED=1 \
    python docs/experiments/diag_runaway_matrix.py <ticket> <ref-sha> "50,240,480" 3
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import sys
import time

from rebar.llm import pai_tools

_CALLS = 0


def _wrap(fn):
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _CALLS
        _CALLS += 1
        return fn(*args, **kwargs)

    wrapper.__signature__ = sig
    return wrapper


def _patch_factory(name: str) -> None:
    orig = getattr(pai_tools, name)
    if getattr(orig, "_diag_patched", False):
        return
    def patched(*a, **k):
        return [_wrap(fn) for fn in orig(*a, **k)]
    patched._diag_patched = True  # type: ignore[attr-defined]
    setattr(pai_tools, name, patched)


class _RunawayWatch(logging.Handler):
    """Records whether the in-flight runaway guard fired during the current run."""

    def __init__(self) -> None:
        super().__init__()
        self.tripped = False
        self.last = ""

    def reset(self) -> None:
        self.tripped = False
        self.last = ""

    def emit(self, record: logging.LogRecord) -> None:
        msg = record.getMessage()
        if "aborted a runaway tool-call loop" in msg:
            self.tripped = True
            self.last = msg


def main() -> None:
    global _CALLS
    ticket = sys.argv[1] if len(sys.argv) > 1 else "f6fc"
    ref = sys.argv[2] if len(sys.argv) > 2 else None
    floors = [int(x) for x in (sys.argv[3] if len(sys.argv) > 3 else "50,240,480").split(",")]
    trials = int(sys.argv[4]) if len(sys.argv) > 4 else 3

    for factory in ("filesystem_tools", "grounding_tools", "rebar_tools"):
        _patch_factory(factory)

    watch = _RunawayWatch()
    logging.getLogger("rebar").addHandler(watch)
    logging.getLogger("rebar.llm.structured_run").addHandler(watch)

    from rebar.llm import completion as _completion
    import rebar

    out = "/tmp/diag_matrix.csv"
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("floor,trial,tool_calls,runaway_tripped,outcome,seconds\n")

    for floor in floors:
        _completion._VERIFY_MIN_STEPS = floor
        os.environ["REBAR_LLM_MAX_STEPS"] = str(floor)
        for trial in range(1, trials + 1):
            _CALLS = 0
            watch.reset()
            t0 = time.time()
            try:
                verdict = rebar.llm.verify_completion(ticket, ref=ref)
                outcome = f"verdict={verdict.get('verdict')}"
            except Exception as exc:  # noqa: BLE001 - we WANT the exhaustion outcome
                outcome = f"raised={type(exc).__name__}"
            secs = round(time.time() - t0, 1)
            row = f"{floor},{trial},{_CALLS},{int(watch.tripped)},{outcome},{secs}"
            print(f"[matrix] {row}", flush=True)
            with open(out, "a", encoding="utf-8") as fh:
                fh.write(row + "\n")

    print(f"[matrix] done -> {out}")


if __name__ == "__main__":
    main()
