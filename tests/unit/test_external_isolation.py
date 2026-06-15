"""Meta-tests for the external-tier isolation safeguards (bug 4a48-6dd5-aef3-4c8e).

These assert the structural guarantees that keep live/billable ``external`` tests
from leaking into a default run, WITHOUT themselves touching any live service:

  (a) With ``REBAR_RUN_EXTERNAL`` unset, collecting tests/external/ yields a run
      where every collected item is SKIPPED (the env opt-in in
      tests/external/conftest.py) — i.e. zero external tests execute by default.
  (b) The confinement invariant holds in the live tree: no test marked
      ``external`` lives outside tests/external/ (enforced at collection by the
      root conftest's pytest_collection_modifyitems).

Both run as ordinary unit tests (network-guarded). (a) invokes pytest as a
subprocess against tests/external/ with a scrubbed env so the result is the
SAME path a default CI run takes.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_EXTERNAL_DIR = _REPO_ROOT / "tests" / "external"


@pytest.mark.allow_network  # subprocess pytest may touch local sockets; we run no live service
def test_external_dir_all_skipped_without_opt_in(tmp_path: Path) -> None:
    """tests/external/ collects but executes NOTHING when REBAR_RUN_EXTERNAL is unset."""
    import os

    env = {k: v for k, v in os.environ.items() if k != "REBAR_RUN_EXTERNAL"}
    # Belt-and-suspenders: present creds must NOT cause execution either.
    env.setdefault("JIRA_URL", "https://example.invalid")
    env.setdefault("JIRA_USER", "probe")
    env.setdefault("JIRA_API_TOKEN", "probe-token")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_EXTERNAL_DIR),
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(tmp_path / "bt"),
            "-rs",
            "-q",
        ],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    # Exit 0 (no failures) and at least one skip, zero passes.
    assert proc.returncode == 0, f"external tier ran/failed without opt-in:\n{out}"
    assert " skipped" in out, f"expected skips in external tier, got:\n{out}"
    assert " passed" not in out, f"external tests EXECUTED without opt-in:\n{out}"


def test_no_external_marked_test_outside_external_dir() -> None:
    """The confinement invariant: every test marked ``external`` is under tests/external/.

    Greps the test tree for the ``external`` marker (decorator or module-level
    ``pytestmark``) and asserts no occurrence lives outside tests/external/. This
    mirrors the hard-fail the root conftest raises at collection, but as a plain
    assertion so a violation is a readable test failure rather than a collection
    abort.
    """
    offenders: list[str] = []
    tests_dir = _REPO_ROOT / "tests"
    this_file = Path(__file__).resolve()
    for path in tests_dir.rglob("test_*.py"):
        resolved = path.resolve()
        if resolved.is_relative_to(_EXTERNAL_DIR):
            continue
        # This meta-test file references the marker name in strings/comments; the
        # authoritative check is the root conftest collection hook, so skip self.
        if resolved == this_file:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "mark.external" in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        "external-marked test(s) found outside tests/external/ — move them under "
        f"tests/external/ (see bug 4a48-6dd5-aef3-4c8e): {offenders}"
    )
