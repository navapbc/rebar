"""Canonical UTC timestamp for the reconciler — twin of ``rebar.timeutils``.

The reconciler runs as a subprocess-isolated, stdlib-only program and imports no
``rebar.*`` modules, so it cannot share ``rebar.timeutils``. This module is a
byte-for-byte twin of :func:`rebar.timeutils.utc_now_iso`; keep the two in
lock-step so timestamps that flow between the library and the reconciler render
identically.
"""

from __future__ import annotations

from datetime import datetime, timezone

__all__ = ["utc_now_iso"]


def utc_now_iso() -> str:
    """Current UTC time as ISO-8601 with a ``Z`` suffix, e.g. ``2026-06-24T19:30:00Z``."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
