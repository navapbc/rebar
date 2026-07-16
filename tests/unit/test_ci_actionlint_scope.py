"""CI guard: `make lint` must actionlint ALL workflows, not just release.yml.

Bug 8002: ``reconcile-bridge.yml`` shipped a job-level ``${{ runner.temp }}`` — a GitHub
Actions *startup failure* (zero jobs, no rebar->Jira sync for ~2 days) — and merged with a
green Gerrit ``Verified`` vote because ``make lint`` only ran actionlint against
``release.yml``. Widening actionlint to every workflow closes that gap.

The existing ``test_ci_workflow_parity.py`` only asserts both CI legs invoke the *string*
``make lint``; it does NOT check actionlint's scope. This test is the guard for that scope:
it fails if the Makefile ``lint`` target is ever re-narrowed to a release.yml-only actionlint,
so the bug-8002 regression cannot recur silently.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _ROOT / "Makefile"


def _lint_recipe() -> str:
    """Return the ``lint:`` target header + its recipe body (tab-indented lines)."""
    lines = _MAKEFILE.read_text().splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("lint:"))
    body = [lines[start]]
    for ln in lines[start + 1 :]:
        if not ln.startswith("\t"):  # first non-recipe line ends the target
            break
        body.append(ln)
    return "\n".join(body)


def test_lint_still_invokes_actionlint() -> None:
    """`make lint` must run actionlint at all (guards against silent removal)."""
    assert "actionlint" in _lint_recipe(), "make lint no longer invokes actionlint"


def test_actionlint_not_scoped_to_release_yml() -> None:
    """No actionlint execution in `make lint` may be scoped to release.yml only.

    An actionlint invoked with ``$(RELEASE_WORKFLOW)`` / a ``release.yml`` path lints a single
    workflow, re-opening the bug-8002 gap. With no path arg, actionlint auto-discovers every
    ``.github/workflows/*.{yml,yaml}`` — which is what CI must do.
    """
    recipe = _lint_recipe()
    # The recipe executes actionlint via "$al" or "$(LOCAL_BIN)/actionlint"; flag either being
    # handed a release.yml-scoped target (directly or through the RELEASE_WORKFLOW variable).
    scoped = re.search(
        r'(?:"\$\$al"|\$\(LOCAL_BIN\)/actionlint"?)\s+(?:\$\(RELEASE_WORKFLOW\)|\S*release\.yml)',
        recipe,
    )
    assert scoped is None, (
        "make lint scopes actionlint to release.yml only — widen it to all workflows "
        f"(bug 8002). Offending fragment: {scoped.group(0) if scoped else None!r}"
    )
