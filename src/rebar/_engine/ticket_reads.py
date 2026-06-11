"""Compat shim: ``ticket_reads`` → ``rebar._engine_support.reads``.

Kept so the engine's bash dispatcher can import the old top-level name (via
the engine dir on ``PYTHONPATH``) even when run by a bare ``python3``. Retire
with the bash→Python strangler-fig ports.
"""

import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parents[2])
if _root not in sys.path:
    sys.path.insert(0, _root)

from rebar._engine_support import reads as _real  # noqa: E402

sys.modules[__name__] = _real

if __name__ == "__main__":
    sys.exit(_real.main(sys.argv[1:]))
