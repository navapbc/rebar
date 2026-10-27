"""Diagnostic: capture WHY the completion verifier runs away.

The durable gate-error record (usage_log.run_shape) deliberately HASHES tool
arguments and drops results, so it can tell us a run issued 164 distinct search_files
calls but never WHAT it searched for or WHAT came back. This script closes that gap
locally and opt-in: it wraps the real completion-verifier tools (filesystem_tools /
grounding_tools / rebar_tools) with a logger that records (tool, args, result-preview,
is_error, is_empty_sentinel) to a LOCAL JSONL, then drives the real
``rebar.llm.verify_completion`` against a known multi-criterion ticket with the step
floor lowered so the run exhausts cheaply (the sanctioned "induced exhaustion" pattern).

Privacy: the raw transcript (which contains repo content from tool results) is written
to a path OUTSIDE the repo and is never committed. Only an aggregated, content-free
analysis is meant to land under docs/experiments. This mirrors run_shape's contract.

Usage:
    AWS_REGION=us-east-1 \
    REBAR_LLM_STANDARD_PROVIDER=bedrock \
    REBAR_LLM_STANDARD_MODEL=us.anthropic.claude-sonnet-4-6 \
    REBAR_LLM_MAX_STEPS=50 \
    python docs/experiments/diag_runaway_transcript.py <ticket-id> [ref-sha]
"""

from __future__ import annotations

import functools
import inspect
import json
import os
import sys
import time

from rebar.llm import pai_tools

TRANSCRIPT_PATH = os.environ.get(
    "DIAG_TRANSCRIPT_PATH", "/tmp/rebar_runaway_transcript.jsonl"
)

_EMPTY_SENTINELS = ("(no matches)", "(empty)", "(empty range)")


def _classify(result: object) -> tuple[bool, bool]:
    text = result if isinstance(result, str) else str(result)
    is_error = text.startswith("Error:") or text.startswith("<RAISED")
    is_empty = text.strip() in _EMPTY_SENTINELS
    return is_error, is_empty


def _wrap(fn):
    sig = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        raised = None
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - diagnostic must see raises too
            result = f"<RAISED {type(exc).__name__}: {exc}>"
            raised = exc
        is_error, is_empty = _classify(result)
        text = result if isinstance(result, str) else str(result)
        rec = {
            "t": round(time.time() - _START, 2),
            "tool": fn.__name__,
            "args": [str(a)[:200] for a in args],
            "kwargs": {k: str(v)[:200] for k, v in kwargs.items()},
            "result_len": len(text),
            "is_error": is_error,
            "is_empty_sentinel": is_empty,
            "result_preview": text[:240],
            "ms": round((time.time() - t0) * 1000),
        }
        with open(TRANSCRIPT_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        if raised is not None:
            raise raised
        return result

    wrapper.__signature__ = sig  # keep pydantic-ai's schema generation intact
    return wrapper


def _patch_factory(name: str) -> None:
    orig = getattr(pai_tools, name)

    def patched(*a, **k):
        return [_wrap(fn) for fn in orig(*a, **k)]

    setattr(pai_tools, name, patched)


def main() -> None:
    global _START
    ticket = sys.argv[1] if len(sys.argv) > 1 else "f6fc"
    ref = sys.argv[2] if len(sys.argv) > 2 else None

    # Truncate any prior transcript.
    open(TRANSCRIPT_PATH, "w", encoding="utf-8").close()

    # Force a cheap, bounded run: lower the verifier step floor so max_iterations
    # (set via REBAR_LLM_MAX_STEPS) is honored instead of being raised to 480.
    from rebar.llm import completion as _completion

    floor = int(os.environ.get("REBAR_LLM_MAX_STEPS", "50"))
    _completion._VERIFY_MIN_STEPS = floor
    print(f"[diag] step floor lowered to {floor}; transcript -> {TRANSCRIPT_PATH}")

    for factory in ("filesystem_tools", "grounding_tools", "rebar_tools"):
        _patch_factory(factory)

    _START = time.time()
    import rebar

    try:
        verdict = rebar.llm.verify_completion(ticket, ref=ref)
        print(f"[diag] verdict returned: {verdict.get('verdict')}")
    except Exception as exc:  # noqa: BLE001 - we WANT the exhaustion outcome
        print(f"[diag] verify raised: {type(exc).__name__}: {exc}")
        diag = getattr(exc, "diagnostic", None)
        if diag:
            print(f"[diag] failure diagnostic: {json.dumps(diag, default=str)[:1200]}")
    print(f"[diag] transcript written to {TRANSCRIPT_PATH}")


_START = 0.0

if __name__ == "__main__":
    main()
