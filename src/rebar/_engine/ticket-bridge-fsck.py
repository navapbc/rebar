#!/usr/bin/env python3
"""Compat shim: ``ticket-bridge-fsck.py`` → ``rebar._engine_support.bridge_fsck``.

Kept so the engine's bash dispatcher can ``exec python3 ticket-bridge-fsck.py``
(run by a bare ``python3`` with the engine dir on PYTHONPATH) even though the
audit logic now lives in-package. Bootstraps ``rebar`` onto ``sys.path`` from this
file's location, then delegates. Retire with the bash dispatcher (Tier E E7).
"""

from __future__ import annotations

import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)

from rebar._engine_support.bridge_fsck import (  # noqa: E402,F401
    audit_bridge_mappings,
    enumerate_duplicate_anomalies,
    enumerate_open_count_skew_anomalies,
    enumerate_orphan_anomalies,
    enumerate_stale_anomalies,
    main,
)

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
