"""Canonical timestamp formatting for rebar.

A single home for "now, as a string" so timestamps render identically everywhere
they are stored or logged. Before this existed, call sites drifted between
``datetime.now(timezone.utc).isoformat()`` (a ``+00:00`` offset, with microseconds)
and ``strftime(...) + "Z"`` (seconds, ``Z`` suffix) — two spellings of the same
instant that parse differently and read inconsistently across logs/records.

The reconciler (``rebar._engine.rebar_reconciler``) is a subprocess-isolated,
stdlib-only program that deliberately imports no ``rebar.*`` modules, so it carries
a byte-for-byte twin of :func:`utc_now_iso` at ``rebar_reconciler/timeutil.py`` —
keep the two in lock-step.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utc_now_iso"]


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with a ``Z`` suffix, e.g. ``2026-06-24T19:30:00Z``.

    Seconds precision (no microseconds) — the common, human- and Jira-friendly form.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
