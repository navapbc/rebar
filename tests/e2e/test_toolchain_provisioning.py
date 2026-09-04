"""The e2e Node toolchain must be provisioned OUTSIDE a test's timeout budget, and its
in-fixture fallback must be bounded, diagnosable and race-safe (bug 9a17-e0b3-7aa6-4091).

The defect these tests pin: ``bpmn_harness`` used to shell out to ``npm`` with no
``timeout=``, inside the first e2e test's setup. The only bound was pytest's global
``timeout = 300`` / ``timeout_method = "thread"``, and that method calls ``os._exit(1)`` —
so a slow npm registry killed the whole xdist worker (``node down: Not properly
terminated``) instead of reporting anything a reader could act on.

Every case here drives a STUB ``npm`` on ``PATH``, so nothing touches the network and the
slow path is reproduced in a second rather than in the ninety-plus that a real cold install
costs. That is what makes the fix verifiable in one run instead of by waiting for a
recurrence.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from _toolchain import ToolchainProvisioningError, provision_toolchain

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "_build-and-test.yml"
_LOCKFILE_HASH_KEY = "tests/e2e/js/package-lock.json"

# Long enough that a stalled step cannot finish on its own, short enough that a test which
# fails to bound it is obvious rather than merely slow.
_STALL_SECONDS = 60
_BOUND = 3.0


def _js_dir(tmp_path: Path, npm_body: str) -> tuple[Path, Path]:
    """A throwaway ``js`` dir with a lockfile, plus a ``bin`` dir holding a stub ``npm``."""
    js_dir = tmp_path / "js"
    js_dir.mkdir()
    (js_dir / "package.json").write_text('{"name":"stub","private":true}\n', encoding="utf-8")
    (js_dir / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npm = bin_dir / "npm"
    npm.write_text(npm_body, encoding="utf-8")
    npm.chmod(0o755)
    return js_dir, bin_dir


def _with_stub_on_path(monkeypatch: pytest.MonkeyPatch, bin_dir: Path) -> None:
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


_SUCCEEDS = """#!/bin/sh
case "$1" in
  ci|install) mkdir -p node_modules && exit 0 ;;
  run) mkdir -p dist && : > dist/roundtrip.mjs && exit 0 ;;
esac
exit 0
"""

_INSTALL_STALLS = f"""#!/bin/sh
case "$1" in
  ci|install) sleep {_STALL_SECONDS} ;;
esac
exit 0
"""

_BUILD_STALLS = f"""#!/bin/sh
case "$1" in
  ci|install) mkdir -p node_modules && exit 0 ;;
  run) sleep {_STALL_SECONDS} ;;
esac
exit 0
"""

_INSTALL_FAILS = """#!/bin/sh
case "$1" in
  ci|install) echo "npm ERR! registry unreachable" >&2; exit 1 ;;
esac
exit 0
"""


def test_provisioning_installs_then_builds_and_leaves_the_bundle_in_place(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path: both steps run, in order, and produce what the fixture needs."""
    js_dir, bin_dir = _js_dir(tmp_path, _SUCCEEDS)
    _with_stub_on_path(monkeypatch, bin_dir)

    provision_toolchain(js_dir)

    assert (js_dir / "node_modules").is_dir()
    assert (js_dir / "dist" / "roundtrip.mjs").is_file()


def test_a_stalled_install_fails_by_name_within_its_own_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hung `npm ci` raises, promptly, naming the step — never a silent worker kill."""
    js_dir, bin_dir = _js_dir(tmp_path, _INSTALL_STALLS)
    _with_stub_on_path(monkeypatch, bin_dir)

    started = time.monotonic()
    with pytest.raises(ToolchainProvisioningError) as caught:
        provision_toolchain(js_dir, install_timeout=1.0, build_timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < _BOUND, f"provisioning ran unbounded for {elapsed:.1f}s"
    message = str(caught.value)
    assert "npm ci" in message
    assert "timed out" in message
    assert "node down" not in message


def test_a_stalled_build_fails_by_name_within_its_own_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The build step carries its own bound and names itself, not the install step."""
    js_dir, bin_dir = _js_dir(tmp_path, _BUILD_STALLS)
    _with_stub_on_path(monkeypatch, bin_dir)

    started = time.monotonic()
    with pytest.raises(ToolchainProvisioningError) as caught:
        provision_toolchain(js_dir, install_timeout=1.0, build_timeout=1.0)
    elapsed = time.monotonic() - started

    assert elapsed < _BOUND, f"provisioning ran unbounded for {elapsed:.1f}s"
    message = str(caught.value)
    assert "npm run build" in message
    assert "timed out" in message


def test_a_failing_install_reports_the_step_and_the_child_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-zero exit is still diagnosable: the step is named and stderr survives."""
    js_dir, bin_dir = _js_dir(tmp_path, _INSTALL_FAILS)
    _with_stub_on_path(monkeypatch, bin_dir)

    with pytest.raises(ToolchainProvisioningError) as caught:
        provision_toolchain(js_dir, install_timeout=10.0, build_timeout=10.0)

    message = str(caught.value)
    assert "npm ci" in message
    assert "registry unreachable" in message


def test_a_missing_npm_is_reported_as_a_named_provisioning_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    js_dir = tmp_path / "js"
    js_dir.mkdir()
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))

    with pytest.raises(ToolchainProvisioningError) as caught:
        provision_toolchain(js_dir)

    assert "npm" in str(caught.value)


_CONCURRENCY_STUB = """#!/bin/sh
# Bracket the install with markers. The file is opened O_APPEND by both writers, so the
# ORDER of lines in it is the order the spans actually happened in.
case "$1" in
  ci|install) ;;
  *) exit 0 ;;
esac
echo start >> ../calls.log
sleep 1
echo end >> ../calls.log
exit 0
"""


def test_two_concurrent_workers_do_not_overlap_their_install_steps(
    tmp_path: Path,
) -> None:
    """Under `-n 4 --dist worksteal` each xdist worker runs the session fixture itself, so
    several can race the same node_modules. The lock makes those spans disjoint."""
    pytest.importorskip("fcntl", reason="the provisioning lock needs POSIX fcntl")
    js_dir, bin_dir = _js_dir(tmp_path, _CONCURRENCY_STUB)
    calls = tmp_path / "calls.log"

    driver = (
        "import sys;"
        f"sys.path.insert(0, {str(Path(__file__).parent)!r});"
        "from _toolchain import provision_toolchain;"
        f"provision_toolchain({str(js_dir)!r}, install_timeout=30.0, build_timeout=30.0)"
    )
    env_path = f"{bin_dir}:/usr/bin:/bin"
    procs = [
        subprocess.Popen(
            [sys.executable, "-c", driver],
            env={"PATH": env_path, "HOME": str(tmp_path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        assert proc.returncode == 0, f"provisioning worker failed: {err or out}"

    ordered = calls.read_text(encoding="utf-8").split()
    assert ordered == ["start", "end", "start", "end"], (
        f"install spans overlapped — the lock did not serialize them: {ordered}"
    )


def _default_suite_job_steps() -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["test"]["steps"]


def test_ci_provisions_the_toolchain_before_the_step_that_runs_the_tests() -> None:
    """Provisioning that runs INSIDE pytest is charged to a test's timeout budget. CI must
    therefore pay for it in its own step, ahead of the one that invokes pytest."""
    steps = _default_suite_job_steps()
    provision = [i for i, s in enumerate(steps) if "e2e-deps" in str(s.get("run", ""))]
    pytest_steps = [i for i, s in enumerate(steps) if "\npytest " in f"\n{s.get('run', '')}"]

    assert provision, "no CI step provisions the e2e Node toolchain"
    assert pytest_steps, "could not find the default-suite pytest step"
    assert min(provision) < min(pytest_steps), (
        "the toolchain provisioning step must precede the pytest step"
    )


def test_ci_caches_the_npm_store_keyed_on_the_e2e_lockfile() -> None:
    steps = _default_suite_job_steps()
    keys = [
        str(s.get("with", {}).get("key", ""))
        for s in steps
        if "actions/cache" in str(s.get("uses", ""))
    ]
    assert any(_LOCKFILE_HASH_KEY in key for key in keys), (
        f"no npm cache keyed on {_LOCKFILE_HASH_KEY}; cache keys seen: {keys}"
    )


def test_the_portable_make_target_provisions_with_npm_ci() -> None:
    """`make e2e-deps` is the no-CI-provider trigger: any checkout can run it, and it uses
    the lockfile-exact `npm ci` rather than `npm install`."""
    completed = subprocess.run(
        ["make", "-n", "e2e-deps"],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, f"`make -n e2e-deps` failed:\n{completed.stderr}"
    recipe = completed.stdout
    assert "npm ci" in recipe, f"e2e-deps does not use `npm ci`:\n{recipe}"
    assert "run build" in recipe, f"e2e-deps does not build the harness bundle:\n{recipe}"
