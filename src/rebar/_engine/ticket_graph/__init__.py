"""Compat shim: ``ticket_graph`` → ``rebar.graph`` (see ticket_reducer shim)."""

import sys
from pathlib import Path

_root = str(Path(__file__).resolve().parents[3])
if _root not in sys.path:
    sys.path.insert(0, _root)

import rebar.graph as _real  # noqa: E402

sys.modules[__name__] = _real

# Mirror already-loaded submodules under the old prefix so
# ``ticket_graph.X is rebar.graph.X`` (see the ticket_reducer shim).
_prefix = _real.__name__
for _name, _mod in list(sys.modules.items()):
    if _name.startswith(_prefix + "."):
        sys.modules[__name__ + _name[len(_prefix):]] = _mod
