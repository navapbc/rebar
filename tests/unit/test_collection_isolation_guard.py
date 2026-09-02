"""Oracle for the unit-tier subprocess-isolation collection guard.

Ticket 15f2-f3d4-ffcd-4223 (washedup-ropeable-hammerkop).

``tests/conftest.py:486-513`` already provides the isolation root: a session-scoped
sandbox repo plus an autouse fixture that sets ``REBAR_ROOT``, per xdist worker. It
reaches subprocesses purely by ENVIRONMENT INHERITANCE, so a test that builds its own
``env=`` dict from scratch silently drops ``REBAR_ROOT`` and its child falls back to the
git toplevel of the cwd — the real checkout.

Nothing stops that today. This guard is the missing enforcement: a collection-time AST
scan, with the predicate factored into ``tests/_subprocess_isolation.py`` so it is
testable without spawning pytest — mirroring the ``_live_jira_confinement`` split that
``pytest_collection_modifyitems`` already uses.

Detection is AST, never regex: a naive ``run(`` pattern matches 126 ``asyncio.run`` call
sites in this tier alone.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "tests" / "_subprocess_isolation.py"
CONFTEST = REPO_ROOT / "tests" / "conftest.py"


def _load():
    spec = importlib.util.spec_from_file_location("_subprocess_isolation", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load()


def _findings(source: str) -> list[dict]:
    return guard.scan_source(source, "tests/unit/test_x.py")


def _kinds(source: str) -> list[str]:
    return [f["kind"] for f in _findings(source)]


def _load():
    spec = importlib.util.spec_from_file_location("_subprocess_isolation", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


guard = _load()


def _kinds(source: str) -> list[str]:
    return [f["kind"] for f in guard.scan_source(source, "tests/unit/test_x.py")]


def test_a_module_with_no_subprocess_call_yields_no_findings():
    source = """
def test_pure():
    assert 1 + 1 == 2
"""
    assert guard.scan_source(source, "tests/unit/test_x.py") == []


def test_a_spawn_with_a_fixture_provided_cwd_is_accepted():
    """The dominant legitimate shape: the root comes from a fixture parameter."""
    source = """
import subprocess

def test_ok(repo):
    subprocess.run(["git", "status"], cwd=repo, check=True)
"""
    assert _kinds(source) == []


def test_a_spawn_with_tmp_path_is_accepted():
    source = """
import subprocess

def test_ok(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True)
"""
    assert _kinds(source) == []


def test_scan_source_reports_findings_as_dicts_with_a_line_number():
    """Each finding carries enough to point a reader at the offending call."""
    source = """
import subprocess

REPO_ROOT = "/repo"

def test_bad():
    subprocess.run(["git", "status"], cwd=REPO_ROOT, check=True)
"""
    findings = guard.scan_source(source, "tests/unit/test_x.py")
    assert findings, "expected the cwd=REPO_ROOT spawn to be reported"
    for f in findings:
        assert isinstance(f["lineno"], int) and f["lineno"] > 0
        assert f["kind"] in {"hazard", "undecidable"}
        assert f["reason"]


# ---------------------------------------------------------------------------
# hazards — must be reported as such
# ---------------------------------------------------------------------------


def test_a_fresh_env_dict_without_the_root_is_a_hazard():
    """3 live sites. The child loses REBAR_ROOT entirely and falls back to the real
    checkout; one of them also points HOME at a bare tmp_path."""
    source = """
import subprocess

def test_bad(tmp_path):
    subprocess.run(["rebar", "show"], env={"PATH": "/usr/bin", "HOME": str(tmp_path)})
"""
    assert "hazard" in _kinds(source)


def test_cwd_pointing_at_the_real_checkout_is_a_hazard():
    """22 live sites spawn against REPO_ROOT."""
    source = """
import subprocess

REPO_ROOT = "/repo"

def test_bad():
    subprocess.run(["python", "-c", "pass"], cwd=REPO_ROOT)
"""
    assert "hazard" in _kinds(source)


def test_git_dash_c_at_the_real_checkout_is_a_hazard():
    """The root can hide in a positional argument, not only in cwd=."""
    source = """
import subprocess

_REPO_ROOT = "/repo"

def test_bad():
    subprocess.run(["git", "-C", str(_REPO_ROOT), "status"])
"""
    assert "hazard" in _kinds(source)


def test_reading_the_operator_home_is_a_hazard():
    """Ticket ec07-692f: a default that resolves to the operator's real home,
    reached transitively — the doctor scan read the operator's own ~/.claude.json."""
    source = """
import subprocess
from pathlib import Path

def test_bad():
    subprocess.run(["ls", str(Path.home())])
"""
    assert "hazard" in _kinds(source)


def test_popen_is_detected_like_run():
    source = """
import subprocess

REPO_ROOT = "/repo"

def test_bad():
    subprocess.Popen(["sleep", "1"], cwd=REPO_ROOT)
"""
    assert "hazard" in _kinds(source)


# ---------------------------------------------------------------------------
# undecidable — reported, never failed
# ---------------------------------------------------------------------------


def test_a_bare_env_variable_is_undecidable_not_a_hazard():
    """~58 live sites pass env=<name> bound elsewhere. Failing these would redden 42
    files on introduction, so the guard must report and move on."""
    source = """
import subprocess

def test_maybe(some_env):
    subprocess.run(["rebar", "show"], env=some_env)
"""
    kinds = _kinds(source)
    assert "undecidable" in kinds
    assert "hazard" not in kinds, "an undecidable site must not be failed"


# ---------------------------------------------------------------------------
# false positives — the direction that makes a guard unusable
# ---------------------------------------------------------------------------


def test_asyncio_run_is_not_a_spawn():
    """126 hits under a naive `run(` regex. The callee must resolve via the file's own
    imports, not by bare attribute name.

    Shaped like the sibling test so misresolution is consequential rather than silent:
    the surrounding module defines REPO_ROOT and the call passes it, so a resolver that
    matches on the bare attribute name reports a hazard.
    """
    source = """
import asyncio

REPO_ROOT = "/repo"

async def main(cwd=None):
    return 1

def test_ok():
    asyncio.run(main(cwd=REPO_ROOT))
"""
    assert _findings(source) == []


def test_a_locally_defined_run_is_not_a_spawn():
    """Deliberately shaped so that MISRESOLVING the callee is consequential.

    A test asserting merely "no findings" cannot distinguish a callee correctly ignored
    from one wrongly matched but harmless — a mutant that resolves every `*.run` to
    `subprocess.run` survives such a test. Here the local `run` is called with
    `cwd=REPO_ROOT`, so a resolver that mistakes it for a real spawn reports a hazard and
    the test fails.
    """
    source = """
REPO_ROOT = "/repo"

def run(cmd, cwd=None):
    return cmd

def test_ok():
    run(["anything"], cwd=REPO_ROOT)
"""
    assert _findings(source) == []


def test_env_built_from_os_environ_is_accepted():
    source = """
import os
import subprocess

def test_ok():
    subprocess.run(["rebar", "show"], env={**os.environ, "REBAR_LOG": "1"})
"""
    assert _kinds(source) == []


def test_a_cherry_picked_env_value_does_not_carry_the_root():
    """A dict literal that pulls ONE variable out of os.environ does not forward
    REBAR_ROOT — the child gets a fresh environment without it and falls back to the git
    toplevel of its cwd, i.e. the real checkout. Only a `**os.environ`-style spread or an
    explicit REBAR_ROOT key actually carries the root, so mentioning `os.environ`
    somewhere in a VALUE must not be read as acceptance."""
    source = """
import os
import subprocess
import sys

def test_bad():
    subprocess.run([sys.executable, "-c", "pass"],
                   env={"PYTHONPATH": "src", "PATH": os.environ.get("PATH", "")})
"""
    assert "hazard" in _kinds(source)


def test_an_env_dict_naming_the_root_explicitly_is_accepted():
    source = """
import subprocess

def test_ok(tmp_path):
    subprocess.run(["rebar", "show"], env={"PATH": "/usr/bin", "REBAR_ROOT": str(tmp_path)})
"""
    assert _kinds(source) == []


def test_the_shared_subprocess_env_helper_is_accepted():
    """tests/_subprocess_env.py is the sanctioned builder and inherits REBAR_ROOT."""
    source = """
import subprocess
from _subprocess_env import subprocess_env

def test_ok(repo):
    subprocess.run(["rebar", "show"], env=subprocess_env(REBAR_ROOT=str(repo)))
"""
    assert _kinds(source) == []


def test_the_nested_pytest_funnel_is_exempt():
    """tests/_nested_pytest.py is the one sanctioned nested-pytest launcher; it already
    pins --basetemp under the caller's tmp_path."""
    source = """
from _nested_pytest import run_nested_pytest

def test_ok(tmp_path):
    run_nested_pytest(tmp_path, ["-q"])
"""
    assert _findings(source) == []


# ---------------------------------------------------------------------------
# the opt-out marker: a reason is mandatory
# ---------------------------------------------------------------------------


def _fake_item(path: Path, name: str, markers: list):
    """The smallest object the predicate reads: a path, a test name, a nodeid, and
    pytest-resolved markers. Markers are resolved through `item.iter_markers`, NOT from
    the AST — so `pytestmark`, class-level marks and inheritance all work the way pytest
    itself resolves them. That is why this is tested here and not through scan_source."""

    class _Marker:
        def __init__(self, args):
            self.args = args

    class _Item:
        def __init__(self):
            self.path = path
            self.name = name
            self.nodeid = f"{path}::{name}"

        def iter_markers(self, marker_name):
            assert marker_name == guard.MARKER_NAME
            return [_Marker(a) for a in markers]

    return _Item()


_HAZARD_MODULE = """
import subprocess

REPO_ROOT = "/repo"


def test_bad():
    subprocess.run(["python", "-c", "pass"], cwd=REPO_ROOT)
"""


def test_a_marker_with_a_reason_admits_the_site(tmp_path):
    """Admission is resolved from real pytest markers on the collected item."""
    module = tmp_path / "test_bad.py"
    module.write_text(_HAZARD_MODULE)
    item = _fake_item(module, "test_bad", [("gate self-test must escape tier monkeypatches",)])
    assert guard.unharnessed_subprocess_reason([item]) is None


def test_an_unmarked_hazard_fails_and_the_message_names_the_fix(tmp_path):
    module = tmp_path / "test_bad.py"
    module.write_text(_HAZARD_MODULE)
    item = _fake_item(module, "test_bad", [])
    reason = guard.unharnessed_subprocess_reason([item])
    assert reason is not None
    assert "test_bad" in reason
    assert guard.MARKER_NAME in reason, "the failure must name the opt-out marker"


@pytest.mark.parametrize("args", [(), ("",), ("   ",)])
def test_a_marker_without_a_real_reason_is_rejected(tmp_path, args):
    """`--strict-markers` validates the marker NAME only, so the mandatory-reason rule
    is the guard's own job — the rule check_config_reads.py applies to `# read-via:`."""
    module = tmp_path / "test_bad.py"
    module.write_text(_HAZARD_MODULE)
    item = _fake_item(module, "test_bad", [args])
    reason = guard.unharnessed_subprocess_reason([item])
    assert reason is not None
    assert "reason" in reason.lower()


# ---------------------------------------------------------------------------
# wiring and the live tier
# ---------------------------------------------------------------------------


def test_the_guard_is_wired_into_collection():
    text = CONFTEST.read_text(encoding="utf-8")
    assert "_subprocess_isolation" in text, "the guard is not called from conftest"


def test_a_controller_side_failure_raises_usage_error_not_pytest_fail():
    """From the xdist controller a pytest.fail() escapes as a 20-line INTERNALERROR and
    buries the message. tests/conftest.py:264-274 already records this for arm (c):
    'a guard whose whole value is a clear message must not look like a crash.'"""
    text = CONFTEST.read_text(encoding="utf-8")
    assert "_subprocess_isolation" in text, "the guard is not called from conftest"
    segment = text[max(0, text.index("_subprocess_isolation") - 2000) :]
    assert "UsageError" in segment, "the guard's failure path must raise pytest.UsageError"


@pytest.mark.allow_unharnessed_subprocess(
    "collects the real committed unit tier; a sandbox copy would assert nothing about it"
)
def test_the_live_unit_tier_collects_clean():
    """Introduction condition (AC2): every real hazard is fixed or explicitly marked, so
    the tier collects. This runs the guard through pytest's own collection rather than
    re-deriving it, which is the only way to exercise the real marker resolution."""
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/unit"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]
