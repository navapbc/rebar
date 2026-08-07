"""Optional-extra coverage guard — bug 599e-77da-29dd-482d.

``pytest.importorskip("fastapi")`` is the right call for a suite that must stay runnable in a
lean install — but it also means a whole test surface can VANISH from CI with nothing
reporting it. That is exactly what happened: no CI lane installed the ``reviewbot`` extra, so
38 tests — the review-bot receiver, the opcert service app, the audit UI, and the
path-injection + token-redaction SECURITY guards — silently no-op'd for months while changes
that broke them still earned ``Verified +1``.

The fix is two-sided. The CI pytest lane now installs every test-bearing extra; this module is
what keeps it that way. When ``REBAR_REQUIRE_EXTRAS=1`` is set (the CI pytest steps set it),
:func:`install` swaps ``pytest.importorskip`` for a strict variant that RAISES instead of
skipping, so a lost extra reddens the build by module name — at collection for a module-scope
call, as a test error for a function-scope one.

Deliberately out of scope: skips keyed on a missing *binary* or a version floor
(``ssh-keygen >= 8.9``, Node for the e2e tier) are ``pytest.mark.skipif``, not
``importorskip``, and stay skippable. The guard covers the one failure mode it is named for —
a test that does not run because nobody installed its Python extra.

Lives next to ``tests/conftest.py`` rather than inside it so the guard is importable (and
therefore directly testable) without re-executing a conftest.
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
