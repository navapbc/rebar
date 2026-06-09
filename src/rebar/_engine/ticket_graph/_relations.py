"""Single source of truth for the blocking-relation vocabulary.

Kept dependency-free on purpose: every module that needs to know which relations
are "blocking" — including the deliberately light-weight ``_ready`` — imports it
from here, so the constant is defined ONCE and no module forks its own copy. (It
cannot live in ``_status``: that module imports the heavy ``_loader``, which
``_ready`` must not pull in.)
"""

from __future__ import annotations

_BLOCKING_RELATIONS = frozenset({"blocks", "depends_on"})
