"""Progress-linked keepalive logging for long LLM gate operations."""

from __future__ import annotations

import logging
import threading
import time

KEEPALIVE_INTERVAL_S = 25.0

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last_emit_at: float | None = None
_call_index = 0


def emit_keepalive(
    phase: str,
    *,
    operation: str | None = None,
    started_at: float | None = None,
) -> bool:
    """Log one coalesced keepalive line at WARNING; return whether it emitted."""
    global _call_index, _last_emit_at

    now = time.monotonic()
    with _lock:
        if _last_emit_at is not None and now - _last_emit_at < KEEPALIVE_INTERVAL_S:
            return False
        _last_emit_at = now
        _call_index += 1
        call_index = _call_index

    elapsed_s = 0.0 if started_at is None else max(0.0, now - started_at)
    op = operation or "unknown"
    logger.warning(
        "llm keepalive phase=%s op=%s call=%d elapsed=%.1fs",
        phase,
        op,
        call_index,
        elapsed_s,
    )
    return True


def _reset_for_tests() -> None:
    global _call_index, _last_emit_at

    with _lock:
        _last_emit_at = None
        _call_index = 0
