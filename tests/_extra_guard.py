"""Make missing Python extras fail collection when CI requires them.

When REBAR_REQUIRE_EXTRAS=1, install replaces pytest.importorskip with an error so a test
surface cannot silently disappear. Binary and version-floor skips remain outside this guard.
The separate module lets its behavior be tested without importing conftest again.
"""

from __future__ import annotations

import os
from typing import Any

import pytest

ENV_VAR = "REBAR_REQUIRE_EXTRAS"

HINT = (
    "{env}=1 is set, so this lane must RUN every test: "
    "`pytest.importorskip({modname!r})` would have SKIPPED it because {modname!r} is not "
    "installed. Add the extra that provides it to this lane's install step "
    "(.github/workflows/_build-and-test.yml), or drop the test. Do not silence this by "
    "unsetting {env} — a silently-skipped test reads as coverage while providing none "
    "(bug 599e-77da-29dd-482d)."
)

#: The unpatched builtin, captured once so :func:`install` is idempotent and reversible.
real_importorskip = pytest.importorskip


def required() -> bool:
    """True when this lane promises to have installed every test-bearing extra."""
    return os.environ.get(ENV_VAR) == "1"


def strict_importorskip(*args: Any, **kwargs: Any) -> Any:
    """``pytest.importorskip`` that FAILS instead of skipping."""
    try:
        return real_importorskip(*args, **kwargs)
    except pytest.skip.Exception as exc:
        modname = args[0] if args else kwargs.get("modname", "<unknown>")
        raise ImportError(HINT.format(env=ENV_VAR, modname=modname)) from exc


def install() -> bool:
    """Arm the guard if this lane asked for it. Returns whether it is now armed."""
    if required():
        pytest.importorskip = strict_importorskip  # type: ignore[assignment]
        return True
    return False
