"""Check repository-verifiable Data Center setup documentation without a Jira harness.

These tests validate install extras and TOML backend selection. Instance-backed command
coverage remains in ``test_user_guide_commands.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

_REPO = Path(__file__).resolve().parents[2]
_USER_GUIDE = _REPO / "docs/user-guide.md"
_PYPROJECT = _REPO / "pyproject.toml"


def _guide() -> str:
    return _USER_GUIDE.read_text()


def test_the_guide_documents_an_installable_extra() -> None:
    """``pip install 'nava-rebar[jira-datacenter]'`` is only correct if the distribution
    really is named ``nava-rebar`` AND really declares a ``jira-datacenter`` extra. Both
    halves are read from pyproject rather than restated, so a rename in either place
    fails here instead of failing for an operator."""
    match = re.search(r"pip install '([a-zA-Z0-9_.-]+)\[([a-zA-Z0-9_.-]+)\]'", _guide())
    assert match, (
        "docs/user-guide.md documents no `pip install '<pkg>[<extra>]'` command for "
        "Data Center — J8 requires the guide to document installing the extra"
    )
    documented_pkg, documented_extra = match.group(1), match.group(2)

    pyproject = tomllib.loads(_PYPROJECT.read_text())
    assert documented_pkg == pyproject["project"]["name"], (
        f"the guide tells operators to install {documented_pkg!r} but the distribution "
        f"is named {pyproject['project']['name']!r} — the documented command would fail"
    )
    extras = pyproject["project"]["optional-dependencies"]
    assert documented_extra in extras, (
        f"the guide documents the extra {documented_extra!r}, which pyproject.toml does "
        f"not declare (has: {sorted(extras)}). The documented install would fail."
    )


def test_the_guide_documents_a_config_block_that_selects_the_dc_backend() -> None:
    """The documented TOML must PARSE and must actually select ``jira-datacenter``.

    This is the block the live test lifts out of the guide and runs, so if it stops
    parsing or stops selecting DC, the live test's premise is gone. Catching that here
    means the failure names the documentation rather than surfacing as a confusing
    config-load error inside a 20-minute live job."""
    blocks = [
        b
        for b in re.findall(r"```toml\n(.*?)```", _guide(), re.DOTALL)
        if "jira-datacenter" in b and "tool.rebar.reconciler" in b
    ]
    assert blocks, (
        "docs/user-guide.md contains no ```toml``` block selecting the 'jira-datacenter' "
        "backend under [tool.rebar.reconciler] — either the DC setup documentation was "
        "removed or its shape changed"
    )
    parsed = tomllib.loads(blocks[0])
    reconciler = parsed["tool"]["rebar"]["reconciler"]
    assert reconciler["backend"] == "jira-datacenter", (
        f"the documented config does not select the DC backend: {reconciler!r}"
    )
    assert reconciler.get("base_url"), "the documented config sets no base_url"


def test_the_guide_states_the_pat_is_environment_only() -> None:
    """The guide presents "JIRA_PAT is env-only, never a config key" as a SECURITY
    property — the credential cannot be committed by accident. The live test verifies the
    behaviour; this pins that the promise is still MADE, so it cannot be quietly dropped
    from the documentation while the test that enforces it keeps passing."""
    guide = _guide()
    assert "JIRA_PAT" in guide, "the guide no longer names the JIRA_PAT variable"
    window = guide[guide.index("JIRA_PAT") : guide.index("JIRA_PAT") + 1200]
    assert "env-only" in window or "environment" in window, (
        "the guide no longer states that JIRA_PAT comes from the environment; the "
        "env-only guarantee is a documented security property and must stay documented"
    )
