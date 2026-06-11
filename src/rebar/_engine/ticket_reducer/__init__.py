"""Compat shim: ``ticket_reducer`` → ``rebar.reducer``.

The reducer is now a real subpackage (``rebar.reducer``); this thin shim keeps
the old top-level name importable for engine subprocesses (bash heredocs +
``ticket-reducer.py``) that run with only the engine dir on ``PYTHONPATH``.
Aliasing the package object preserves identity (``ticket_reducer is
rebar.reducer``) so submodule lookups resolve to the real package dir. Retire
with the bash→Python strangler-fig ports that drop the old import names.
"""

import sys
from pathlib import Path

# Engine subprocesses may run a bare ``python3`` that only has the engine dir on
# its import path, so make the ``rebar`` package importable from this shim's own
# location (no-op when ``rebar`` is already importable, e.g. a wheel install).
_root = str(Path(__file__).resolve().parents[3])
if _root not in sys.path:
    sys.path.insert(0, _root)

import rebar.reducer as _real  # noqa: E402

sys.modules[__name__] = _real

# Mirror the real package's already-loaded submodules under the old prefix so
# ``ticket_reducer.X is rebar.reducer.X`` (otherwise a fresh ``import
# ticket_reducer.X`` would load a second copy and break re-export identity).
_prefix = _real.__name__
for _name, _mod in list(sys.modules.items()):
    if _name.startswith(_prefix + "."):
        sys.modules[__name__ + _name[len(_prefix):]] = _mod
