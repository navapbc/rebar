"""The DOCUMENTED Data Center setup, executed against the J5 harness (story J8, epic e369).

J8's acceptance criterion is not "the DC section exists in the user guide" — it is that
**the documented commands have been executed against the harness**. Prose that has never
been run is a hypothesis, and a setup guide is exactly the artifact where an untested
hypothesis costs an operator an afternoon: they follow it literally, it fails, and they
have no way to tell whether the guide or their instance is wrong.

WHAT MAKES THIS DIFFERENT FROM ``test_reconcile_pass.py``. That module drives the
reconciler through a config *the test itself* composes — the shape a rebar developer knows
to write. This one drives it through the config shape **lifted out of `docs/user-guide.md`
at runtime**, so the guide and the code cannot drift apart silently. If someone edits the
documented TOML into something that no longer selects the DC backend, this fails; if
someone renames a config key, this fails. A hand-copied duplicate of the doc would pass
happily while the doc rotted, which is why the block is PARSED rather than restated.

TWO SURFACES, AND WHY THIS ONE. rebar reads bare ``[reconciler]`` from a standalone
``rebar.toml`` and ``[tool.rebar.reconciler]`` from a ``pyproject.toml``
(``_config_sources.py:153-159``). The guide documents the ``[tool.rebar.…]`` form, so this
writes a **pyproject.toml** — the surface where that table name is the correct one.
Testing the documented spelling against the file it actually applies to is the whole point;
rewriting it into the other form would prove the guide works by not using it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_BASE = "http://localhost:2990/jira"
_USER_GUIDE = Path(__file__).resolve().parents[3] / "docs/user-guide.md"


def _live_jira_ready() -> bool:
    """The sentinel ``tests/external/conftest.py`` keys on to apply ``jira_live``."""
    try:
        req = urllib.request.Request(f"{_BASE.rstrip('/')}/rest/api/2/serverInfo")
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _jira_extra_installed() -> bool:
    try:
        import jira  # noqa: F401
    except ImportError:
        return False
    return True


_skip = pytest.mark.skipif(
    not _live_jira_ready(),
    reason=(
        f"Jira DC harness not reachable at {_BASE} — start it with `make jira-dc-up` "
        "and run with REBAR_RUN_EXTERNAL=1"
    ),
)

_extra_missing_but_harness_up = _live_jira_ready() and not _jira_extra_installed()


@pytest.fixture(autouse=True)
def _fail_if_extra_missing_while_harness_is_up() -> None:
    """Harness up + extra absent is a BROKEN ENVIRONMENT, not a skip: the all-skip
    canary counts globally per session, so a sibling module's executing tests would
    mask this module skipping entirely and the job would report green."""
    if _extra_missing_but_harness_up:
        pytest.fail(
            f"the Jira DC harness is reachable at {_BASE} but the 'jira-datacenter' "
            "extra is NOT installed, so this module would silently skip and certify "
            "documentation that was never executed. Install: pip install -e "
            "'.[dev,jira-datacenter]'"
        )


# ---------------------------------------------------------------------------
# Lifting the documented config out of the guide
# ---------------------------------------------------------------------------


def _documented_toml_block() -> str:
    """Return the ``[tool.rebar.reconciler]`` TOML block the user guide documents.

    Parsed from the guide rather than restated, so this test measures the DOCUMENT.
    A restated copy would keep passing while the guide drifted — the precise failure
    this criterion exists to prevent.
    """
    text = _USER_GUIDE.read_text()
    for block in re.findall(r"```toml\n(.*?)```", text, re.DOTALL):
        if "jira-datacenter" in block and "tool.rebar.reconciler" in block:
            return block
    raise AssertionError(
        "docs/user-guide.md contains no ```toml``` block selecting the "
        "'jira-datacenter' backend under [tool.rebar.reconciler]. Either the DC "
        "setup documentation was removed or its shape changed — J8 requires the "
        "guide to document selecting the DC backend."
    )


# NOTE — the harness-FREE half of this criterion (does the documented `pip install`
# name an extra that exists?) deliberately does NOT live here. Every test in this
# directory inherits the session-scoped autouse `_jira_dc_harness_ready` fixture, which
# waits out the full 20-minute readiness budget and then raises when no harness is
# present. An unmarked test in this module therefore does not "run without the harness"
# — it blocks for 20 minutes and errors (measured: 1208s). That check is a pure
# repo-vs-repo comparison needing no network, so it lives in
# `tests/unit/test_user_guide_dc_docs.py`, where it runs on every change.


# ---------------------------------------------------------------------------
# Executing them against the harness
# ---------------------------------------------------------------------------


@pytest.fixture
def documented_repo(
    rebar_repo: Path,
    jira_dc_project: str,
    jira_dc_pat: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Path:
    """A rebar repo configured EXACTLY as ``docs/user-guide.md`` instructs.

    Three substitutions are made into the documented block, and each is itself
    documented in the same section of the guide, so this stays a test of the guide
    rather than of a variant of it:

    * ``base_url`` — the guide's value is an illustrative
      ``https://jira.internal.example.gov``; it is pointed at the harness.
    * ``project`` — pointed at the scratch project this test owns.
    * ``allow_insecure = true`` — the guide documents this key as the supported
      answer for a non-``https`` instance and names a loopback test instance as its
      intended use. The harness is plain http, so following the guide for THIS
      deployment means setting it. Without it, config load correctly rejects the URL.

    ``JIRA_PAT`` is exported rather than written to config because the guide says it is
    env-only and never a config key — asserted below, since that is a security property.
    """
    block = _documented_toml_block()
    block = re.sub(r'base_url\s*=\s*"[^"]*"', f'base_url = "{_BASE}"', block)
    block = re.sub(r'project\s*=\s*"[^"]*"', f'project = "{jira_dc_project}"', block)
    block = block.replace("[tool.rebar.jira]", "allow_insecure = true\n\n[tool.rebar.jira]")

    (rebar_repo / "pyproject.toml").write_text(block)
    monkeypatch.setenv("JIRA_PAT", jira_dc_pat)
    return rebar_repo


@_skip
def test_the_documented_config_and_preview_work_against_a_real_dc_instance(
    documented_repo: Path,
) -> None:
    """THE CRITERION: `rebar bridge preview`, run exactly as documented, against a
    real Jira Data Center instance configured exactly as documented.

    Asserts on the ENVELOPE, not merely the exit code. ``preview`` is non-mutating,
    and no-write modes are the ones that emit JSON on stdout (``__main__.py:445-452``);
    a run that exited 0 having produced no envelope did not complete a pass.
    """
    from rebar._engine import engine_env

    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar_reconciler",
            "preview",
            "--repo-root",
            str(documented_repo),
        ],
        env=engine_env(str(documented_repo)),
        text=True,
        capture_output=True,
        check=False,
    )

    assert "Traceback (most recent call last)" not in cp.stderr, (
        f"the documented preview raised an unhandled exception:\n{cp.stderr}"
    )
    assert cp.returncode == 0, (
        f"the documented setup did not produce a working preview "
        f"(exit {cp.returncode}).\n--stdout--\n{cp.stdout}\n--stderr--\n{cp.stderr}"
    )

    envelope = None
    for line in reversed([ln for ln in cp.stdout.splitlines() if ln.strip()]):
        try:
            envelope = json.loads(line)
            break
        except json.JSONDecodeError:
            continue
    assert envelope is not None, (
        f"no JSON envelope on stdout — the preview exited 0 without completing a "
        f"pass:\n{cp.stdout}\n--stderr--\n{cp.stderr}"
    )
    assert envelope.get("no_write") is True, (
        f"the documented preview was not non-mutating: {envelope!r}. "
        f"A guide that tells operators to 'inspect before enabling live sync' and then "
        f"writes would be actively dangerous."
    )
    assert envelope.get("mutation_failures", 0) == 0, (
        f"the documented preview reported mutation failures: {envelope!r}"
    )


@_skip
def test_the_documented_setup_refuses_to_read_the_pat_from_config(
    documented_repo: Path, jira_dc_pat: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guide states JIRA_PAT is "env-only, never a config key" and presents that as a
    SECURITY property — the credential cannot be committed by accident. A documented
    security guarantee that nothing checks is the weakest kind of claim, so it is checked
    here against the real instance rather than trusted.

    Removing it from the environment must fail with an error NAMING the variable, and must
    not fall back to anonymous access — an anonymous fallback would turn a missing
    credential into a silently empty pass.
    """
    from rebar._engine import engine_env

    pyproject = documented_repo / "pyproject.toml"
    pyproject.write_text(pyproject.read_text() + f'\njira_pat = "{jira_dc_pat}"\n')
    monkeypatch.delenv("JIRA_PAT", raising=False)

    env = engine_env(str(documented_repo))
    env.pop("JIRA_PAT", None)
    cp = subprocess.run(
        [
            sys.executable,
            "-m",
            "rebar_reconciler",
            "preview",
            "--repo-root",
            str(documented_repo),
        ],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert cp.returncode != 0, (
        "the reconciler succeeded with JIRA_PAT absent from the environment and present "
        "only in config — either the credential was read from the config file (which the "
        "guide promises cannot happen) or the pass ran anonymously"
    )
    assert "JIRA_PAT" in (cp.stderr + cp.stdout), (
        f"the failure does not name JIRA_PAT, so an operator cannot tell what is "
        f"missing:\n--stdout--\n{cp.stdout}\n--stderr--\n{cp.stderr}"
    )
