"""Execute the documented Data Center setup against the J5 harness (J8, epic e369).

The test parses the ``[tool.rebar.reconciler]`` block from the user guide at runtime and
writes it to the documented ``pyproject.toml`` surface. Running that exact configuration
and command shape prevents the guide and implementation from drifting behind a copied fixture.
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
    """Parse the documented DC TOML block so the guide itself remains the test subject."""
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


# Extra-name validation stays in the harness-free unit suite; this directory's autouse
# readiness fixture would otherwise wait and fail when Jira is absent.


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
    """Apply only the guide's documented harness substitutions.

    Replace the example URL and project, enable ``allow_insecure`` for loopback, and export
    ``JIRA_PAT`` as the guide's env-only credential rather than writing it to config.
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
    """Run the documented preview against real DC and require its non-mutating envelope.

    Exit zero without the no-write JSON result does not prove a completed pass.
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
    """Require ``JIRA_PAT`` from the environment, never a config key.

    Removing it must fail with an error naming the variable rather than falling back to the
    injected config value or anonymous access.
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
