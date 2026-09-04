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

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
import yaml
from _toolchain import LOCK_NAME, ToolchainProvisioningError, provision_toolchain

# Drives the REAL conftest through a REAL collection, rather than calling its hook by
# hand — the hook only means anything in the context pytest gives it.
pytest_plugins = ["pytester"]

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

_SUCCEEDS_LOGGING = """#!/bin/sh
echo "$*" >> npm-calls.log
case "$1" in
  ci|install)
    mkdir -p node_modules
    case "$*" in *--omit=optional*) ;; *) mkdir -p node_modules/playwright ;; esac
    exit 0 ;;
  run) mkdir -p dist && : > dist/roundtrip.mjs && exit 0 ;;
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
# Bracket BOTH provisioning steps with markers. The file is opened O_APPEND by every writer,
# so the ORDER of lines in it is the order the spans actually happened in. Nothing here
# creates node_modules or the bundle, so both processes really run both steps — which is
# what gives the disjointness assertion something to observe.
case "$1" in
  ci|install) step=install ;;
  run) step=build ;;
  *) exit 0 ;;
esac
echo "${step}-start" >> ../calls.log
sleep 1
echo "${step}-end" >> ../calls.log
exit 0
"""


def test_two_concurrent_workers_do_not_overlap_their_provisioning_steps(
    tmp_path: Path,
) -> None:
    """Under `-n 4 --dist worksteal` each xdist worker runs the session fixture itself, so
    several can race the same node_modules/dist. The lock makes every span disjoint — the
    build as much as the install, since the build is the step that writes the bundle a peer
    might otherwise observe half-written."""
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
    assert len(ordered) == 8, f"expected two install+build pairs from two processes: {ordered}"
    spans = [(ordered[i], ordered[i + 1]) for i in range(0, len(ordered), 2)]
    assert all(end == start.replace("-start", "-end") for start, end in spans), (
        f"a span opened while another was still open — the lock did not serialize them: {ordered}"
    )
    assert [start for start, _ in spans] == ["install-start", "build-start"] * 2, ordered


# ---------------------------------------------------------------------------
# the forced interleaving
#
# The race this pins is not reproduced by running N workers and hoping. The interleaving is
# CONSTRUCTED: a peer process takes the provisioning lock and, while holding it, creates
# exactly the state an unlocked readiness check inspects — node_modules, the browser stack,
# and a `dist/roundtrip.mjs` that exists but is half-written, which is what esbuild leaves
# visible while it writes the bundle in place. Only then does the caller run. The append-only
# log is the oracle, so the verdict is an ORDER, not a duration.
# ---------------------------------------------------------------------------

_PEER_HOLD_SECONDS = 2.0
_PEER_MID_BUILD = "peer-mid-build"

_PEER_HOLDS_A_HALF_WRITTEN_BUNDLE = r"""
import fcntl
import sys
import time
from pathlib import Path

js_dir, log = Path(sys.argv[1]), Path(sys.argv[2])
lock_name, hold = sys.argv[3], float(sys.argv[4])
handle = (js_dir / lock_name).open("a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
try:
    (js_dir / "node_modules" / "playwright").mkdir(parents=True, exist_ok=True)
    (js_dir / "dist").mkdir(parents=True, exist_ok=True)
    # esbuild writes the bundle IN PLACE: the path exists long before the bytes are complete.
    (js_dir / "dist" / "roundtrip.mjs").write_text("// half-written\n", encoding="utf-8")
    with log.open("a", encoding="utf-8") as fh:
        fh.write("peer-mid-build\n")
    time.sleep(hold)
    with log.open("a", encoding="utf-8") as fh:
        fh.write("peer-done\n")
finally:
    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    handle.close()
"""


def _await_line(log: Path, needle: str, peer: subprocess.Popen, *, deadline_s: float) -> None:
    """Block until the peer has recorded `needle` — a PRECONDITION, not a hoped-for race."""
    limit = time.monotonic() + deadline_s
    while time.monotonic() < limit:
        if log.is_file() and needle in log.read_text(encoding="utf-8"):
            return
        assert peer.poll() is None, f"the peer exited before recording {needle!r}"
        time.sleep(0.02)
    raise AssertionError(f"the peer never recorded {needle!r} within {deadline_s:g}s")


def test_a_caller_cannot_return_while_a_peer_holds_the_lock_over_a_half_written_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, forced: provisioning must not answer "ready" from outside the lock.

    A pre-lock `_satisfied()` sees node_modules and an existing `dist/roundtrip.mjs` and
    returns AT ONCE, while the peer holding the lock is still writing that file — so the
    caller would go on to run `node dist/roundtrip.mjs` against a truncated bundle. With the
    check under the lock the caller can only return after the peer releases it, which is a
    fact about ORDER and so is decidable in one run rather than by repetition.
    """
    pytest.importorskip("fcntl", reason="the provisioning lock needs POSIX fcntl")
    js_dir, bin_dir = _js_dir(tmp_path, _SUCCEEDS)
    _with_stub_on_path(monkeypatch, bin_dir)
    log = tmp_path / "order.log"
    peer_script = tmp_path / "peer.py"
    peer_script.write_text(_PEER_HOLDS_A_HALF_WRITTEN_BUNDLE, encoding="utf-8")

    peer = subprocess.Popen(
        [
            sys.executable,
            str(peer_script),
            str(js_dir),
            str(log),
            LOCK_NAME,
            str(_PEER_HOLD_SECONDS),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _await_line(log, _PEER_MID_BUILD, peer, deadline_s=30.0)
        provision_toolchain(js_dir, install_timeout=30.0, build_timeout=30.0)
        with log.open("a", encoding="utf-8") as fh:
            fh.write("caller-returned\n")
    finally:
        out, err = peer.communicate(timeout=60)
    assert peer.returncode == 0, f"the peer failed: {err or out}"

    ordered = log.read_text(encoding="utf-8").split()
    assert ordered == ["peer-mid-build", "peer-done", "caller-returned"], (
        "the caller returned while a peer still held the lock over a half-written bundle — "
        f"a readiness check ran outside the lock: {ordered}"
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


# ---------------------------------------------------------------------------
# the collection hook, driven through a real pytest run
#
# The hook is the load-bearing half of the fix, so it is exercised end to end: a replica of
# the real tests/e2e tree (the SAME conftest.py and _toolchain.py, copied so their
# module-relative `js` directory lands in the sandbox) is collected by a nested pytest with
# stub `node`/`npm` on PATH. Every assertion below is on what that run OBSERVABLY did —
# outcomes, skip reasons, files created, and how many times `npm` was invoked — never on the
# module-level flags that produce them.
# ---------------------------------------------------------------------------

_E2E_DIR = Path(__file__).parent

_STUB_NODE = """#!/bin/sh
exit 0
"""

_NPM_SUCCEEDS = """#!/bin/sh
echo "$*" >> "{log}"
case "$1" in
  ci|install)
    mkdir -p node_modules
    case "$*" in *--omit=optional*) ;; *) mkdir -p node_modules/playwright ;; esac
    exit 0 ;;
  run) mkdir -p dist && : > dist/roundtrip.mjs && exit 0 ;;
esac
exit 0
"""

_NPM_FAILS = """#!/bin/sh
echo "$*" >> "{log}"
case "$1" in
  ci|install) echo "npm ERR! registry unreachable" >&2; exit 1 ;;
esac
exit 0
"""

_NEEDS_HARNESS = """
def test_needs_the_harness(bpmn_harness):
    assert bpmn_harness is not None
"""

_NEEDS_BROWSER = """
def test_needs_the_browser(browser_runner):
    assert browser_runner is not None
"""

_NEEDS_NOTHING = """
def test_needs_no_toolchain():
    assert True
"""


def _replica(pytester: pytest.Pytester, npm_body: str, **test_files: str) -> tuple[Path, Path]:
    """Stand up a copy of the real e2e tree in the sandbox; return its js dir and npm log."""
    root = pytester.path
    shutil.copy(_E2E_DIR.parent / "_child_diag.py", root / "_child_diag.py")
    e2e = root / "e2e"
    e2e.mkdir()
    for name in ("conftest.py", "_toolchain.py"):
        shutil.copy(_E2E_DIR / name, e2e / name)
    js_dir = e2e / "js"
    js_dir.mkdir()
    (js_dir / "package.json").write_text('{"name":"stub","private":true}\n', encoding="utf-8")
    (js_dir / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    for name, body in test_files.items():
        (e2e / f"{name}.py").write_text(body, encoding="utf-8")

    log = root / "npm-calls.log"
    bin_dir = root / "bin"
    bin_dir.mkdir()
    for name, body in (("npm", npm_body.format(log=log)), ("node", _STUB_NODE)):
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
    return js_dir, log


def _npm_calls(log: Path) -> list[str]:
    """One entry per `npm` invocation, each the full argument list it was given."""
    if not log.is_file():
        return []
    return [line for line in log.read_text(encoding="utf-8").splitlines() if line]


def _run(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *args: str):
    monkeypatch.setenv("PATH", f"{pytester.path / 'bin'}:/usr/bin:/bin")
    return pytester.runpytest_subprocess("e2e", "-p", "no:cacheprovider", *args)


def test_collection_alone_provisions_the_toolchain(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(a) `--collect-only` provisions — which is the whole point of the fix.

    Collecting runs no test, so nothing here can be charged to a test's pytest-timeout
    budget. This is also what separates the hook from the fixture fallback: a fallback that
    provisioned inside the fixture would leave `--collect-only` with nothing installed.
    """
    js_dir, log = _replica(pytester, _NPM_SUCCEEDS, test_harness=_NEEDS_HARNESS)

    result = _run(pytester, monkeypatch, "--collect-only")

    assert result.ret == 0
    result.assert_outcomes()  # no test ran at all
    assert (js_dir / "node_modules").is_dir()
    assert (js_dir / "dist" / "roundtrip.mjs").is_file()
    assert _npm_calls(log) == ["ci --omit=optional", "run build"], (
        "a selection with no browser test must not install the optional browser stack"
    )


def test_collection_does_not_provision_when_no_selected_test_needs_it(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(b) A selection with no toolchain test pays nothing — npm is never invoked.

    This is what keeps the toolchain's own tests and the macOS platform_compat subset off the
    hook: provisioning follows the SELECTION, not the mere presence of the conftest.
    """
    js_dir, log = _replica(pytester, _NPM_SUCCEEDS, test_plain=_NEEDS_NOTHING)

    result = _run(pytester, monkeypatch, "--collect-only")

    assert result.ret == 0
    assert not (js_dir / "node_modules").exists()
    assert _npm_calls(log) == []


def test_provisioning_happens_once_for_a_whole_selection(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(c) Idempotence: many toolchain tests, one install — the fixture never re-provisions."""
    _js_dir, log = _replica(
        pytester,
        _NPM_SUCCEEDS,
        test_harness=_NEEDS_HARNESS,
        test_browser=_NEEDS_BROWSER,
        test_more=_NEEDS_HARNESS.replace("test_needs_the_harness", "test_also_needs_it"),
    )

    result = _run(pytester, monkeypatch)

    result.assert_outcomes(passed=3)
    installs = [call for call in _npm_calls(log) if call.startswith("ci")]
    assert installs == ["ci"], (
        "one install for the whole selection, and it must include the browser stack "
        f"because a browser test is selected; got {installs}"
    )


def test_a_collection_failure_is_named_by_both_fixtures_and_never_retried(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(d) A failed provisioning is re-REPORTED, not re-RUN, by every toolchain fixture.

    Both fixtures must name it: reporting it for one and not the other is the asymmetric
    surface this change exists to remove. And a slow failure must be paid once, not once per
    test — which is why the count assertion matters as much as the messages.
    """
    _js_dir, log = _replica(
        pytester, _NPM_FAILS, test_harness=_NEEDS_HARNESS, test_browser=_NEEDS_BROWSER
    )

    result = _run(pytester, monkeypatch, "-rs")

    result.assert_outcomes(skipped=2)
    result.stdout.fnmatch_lines(["*e2e: e2e toolchain: `npm ci` failed*"])
    result.stdout.fnmatch_lines(["*e2e(browser): e2e toolchain: `npm ci` failed*"])
    installs = [call for call in _npm_calls(log) if call.startswith("ci")]
    assert len(installs) == 1, f"a failed provisioning was retried per test: {installs}"


def test_a_tree_without_a_lockfile_falls_back_to_npm_install(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`npm ci` needs a lockfile to be exact about; without one, `npm install` is the path."""
    js_dir, bin_dir = _js_dir(tmp_path, _SUCCEEDS_LOGGING)
    (js_dir / "package-lock.json").unlink()
    _with_stub_on_path(monkeypatch, bin_dir)

    provision_toolchain(js_dir)

    assert _npm_calls(js_dir / "npm-calls.log") == ["install", "run build"]


def test_a_browser_request_completes_a_tree_installed_without_the_browser_stack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A harness-only install must not look "done" to a later caller that needs the browser.

    Without this, a session that provisioned for `bpmn_harness` would leave `node_modules`
    in place and the next browser run would skip citing a missing playwright — the real
    cause (the browser stack was deliberately omitted) never surfacing.
    """
    js_dir, bin_dir = _js_dir(tmp_path, _SUCCEEDS_LOGGING)
    _with_stub_on_path(monkeypatch, bin_dir)
    provision_toolchain(js_dir, with_browser=False)
    assert not (js_dir / "node_modules" / "playwright").exists()

    provision_toolchain(js_dir, with_browser=True)

    calls = _npm_calls(js_dir / "npm-calls.log")
    assert calls == ["ci --omit=optional", "run build", "ci"], calls


def test_a_satisfied_tree_is_not_reinstalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal case after `make e2e-deps`: provisioning is a no-op, so it costs nothing."""
    js_dir, bin_dir = _js_dir(tmp_path, _SUCCEEDS_LOGGING)
    _with_stub_on_path(monkeypatch, bin_dir)
    provision_toolchain(js_dir, with_browser=True)
    before = _npm_calls(js_dir / "npm-calls.log")

    provision_toolchain(js_dir, with_browser=True)

    assert _npm_calls(js_dir / "npm-calls.log") == before


def test_an_unusable_lock_directory_is_a_named_provisioning_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provisioning reports every failure as its own error kind, never a raw OSError.

    The collection hook records a `ToolchainProvisioningError` and lets the fixtures skip;
    anything else escaping would abort COLLECTION, taking down tests that have nothing to do
    with the e2e tier.
    """
    js_dir, bin_dir = _js_dir(tmp_path, _SUCCEEDS)
    _with_stub_on_path(monkeypatch, bin_dir)
    # A directory where the lock file must live makes opening it fail.
    (js_dir / LOCK_NAME).mkdir()

    with pytest.raises(ToolchainProvisioningError) as caught:
        provision_toolchain(js_dir)

    assert "lock" in str(caught.value)


def test_the_portable_target_skips_cleanly_when_node_is_absent(tmp_path: Path) -> None:
    """`make e2e-deps` on a host without Node exits 0 and says so.

    The e2e tier is self-skipping by design, so a machine with no Node is not a build
    failure — and CI runs this target, so making it fail there would break every build on a
    runner whose image lost Node.
    """
    make = shutil.which("make")
    assert make, "this test needs `make`"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "make").symlink_to(make)

    completed = subprocess.run(
        [str(bin_dir / "make"), "e2e-deps"],
        cwd=_REPO_ROOT,
        env={"PATH": str(bin_dir)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert completed.returncode == 0, f"absent Node must not fail the build:\n{completed.stderr}"
    assert "skipping" in completed.stdout


def test_ci_tolerates_a_failed_pre_provision_and_says_so() -> None:
    """A failed pre-provision must degrade to the in-fixture path, not fail the CI step.

    Pre-provisioning is an optimisation: it moves the install off a test's clock. When it
    cannot run, the portable in-process path is still correct, so failing the step would
    turn this fix into a red vote on unrelated changes.
    """
    step = next(s for s in _default_suite_job_steps() if "e2e-deps" in str(s.get("run", "")))
    body = str(step["run"])

    assert "make e2e-deps\n" not in body, (
        "`make e2e-deps` is invoked unguarded — a transient npm failure would fail the step"
    )
    assert "! make e2e-deps" in body
    assert body.count("::notice::") >= 3, (
        "every skip path must be visible, or a silent skip reads as success"
    )
