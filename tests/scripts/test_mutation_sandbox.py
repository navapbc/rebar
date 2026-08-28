"""Tests for the mutation-gate OS sandbox [rebar:e668-b496-e264-4283].

The sandbox is the only control a mutation cannot delete: the harness
string-substitutes the artifact under test, so any guard *inside* that artifact is
mutable, while an OS-enforced write-deny around the subprocess is not.

These tests observe **enforcement**, not construction. Asserting the wrapper argv
alone would pass against a sandbox that silently denies nothing — which is the exact
failure mode the fail-closed design exists to prevent.
"""

from __future__ import annotations

import ast
import importlib.util
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import SubprocessEnv, subprocess_env

# `platform_compat` is load-bearing, not decorative. The macOS gate cell selects
# `platform_compat and not external`; without it these tests are `unit`-only and are
# selected by no gating lane, while on Linux `@live` skips them because no workflow
# installs bubblewrap. That combination left the sandbox ENFORCEMENT unverified in CI
# entirely — flagged by code review on change 2256.
pytestmark = [pytest.mark.unit, pytest.mark.platform_compat]

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("mutation_sandbox")


@pytest.fixture(autouse=True)
def _clear_probe_cache():
    """`probe()` is lru_cached, so a monkeypatched `which` would otherwise be ignored.

    Clearing before AND after also stops one test's patched mechanism leaking into
    the next, which would silently make an enforcement test assert nothing.
    """
    getattr(sb.probe, "cache_clear", lambda: None)()
    yield
    getattr(sb.probe, "cache_clear", lambda: None)()


def _why(proc: subprocess.CompletedProcess[str]) -> str:
    """Render the sandbox subprocess outcome for an assertion message."""
    return f"exit={proc.returncode} stderr={(proc.stderr or '').strip()!r}"


# The lane that exists to PROVE Linux enforcement sets this. There, a skip is a
# FAILURE: a green leg full of skipped enforcement tests is the silent-skip mode this
# flag removes (`0d22-f664`). Everywhere else the skip stands, so a developer without a
# mechanism is not blocked.
REQUIRE_ENFORCEMENT = os.environ.get("REBAR_REQUIRE_SANDBOX_ENFORCEMENT") == "1"

live = pytest.mark.skipif(
    sb.probe() is None and not REQUIRE_ENFORCEMENT,
    reason="no OS sandbox mechanism on this host (sandbox-exec or bwrap)",
)


def test_a_mechanism_is_required_when_the_lane_says_so():
    """On the enforcement lane, a missing mechanism fails LOUDLY instead of skipping.

    Without this the lane could go green having proven nothing — bubblewrap installed,
    every enforcement test skipped, the Linux write-deny still unverified.
    """
    if not REQUIRE_ENFORCEMENT:
        pytest.skip("REBAR_REQUIRE_SANDBOX_ENFORCEMENT is not set on this lane")
    assert sb.probe() is not None, (
        "REBAR_REQUIRE_SANDBOX_ENFORCEMENT=1 but no OS sandbox mechanism is available: "
        "this lane exists to prove enforcement, so a skip here is a failure. On Linux "
        "bwrap needs an unprivileged user namespace — see the lane's precondition step."
    )


# --- enforcement safety -----------------------------------------------------------
# An enforcement test asserts that a control DENIES something. If the control silently
# does nothing, the denied action HAPPENS — the 2026-08-26 incident's own mechanism,
# re-created inside the test suite. Three rules keep these tests fail-safe:
#
#   1. Prove denial with a CREATE wherever the proof allows it. A leaked create is
#      inert; a leaked delete destroys. Only the incident-shape test removes anything.
#   2. Paths reach the shell through the ENVIRONMENT and are dereferenced as
#      "${VAR:?}" — never interpolated into the script text. An empty or unset value
#      then aborts the shell instead of expanding `rm -rf "$EMPTY"/*` from the root,
#      which is exactly the guard whose absence caused the incident.
#   3. Python confirms containment BEFORE launching a destructive child, rather than
#      trusting the sandbox it is testing.

LIVENESS = "child-reached-the-write"


def _inside(tmp_path: Path, name: str) -> Path:
    """Return `tmp_path/name`, refusing anything not strictly inside `tmp_path`.

    The destructive test hands its target to `rm -rf`. If the sandbox enforces nothing
    that removal really happens, so containment is checked here rather than assumed.
    """
    if not name:
        raise AssertionError("refusing an empty target name")
    target = (tmp_path / name).resolve()
    root = tmp_path.resolve()
    if target == root or root not in target.parents:
        raise AssertionError(f"refusing {target!r}: not strictly inside {root!r}")
    return target


def _assert_child_ran(sentinel: Path, proc: subprocess.CompletedProcess[str]) -> None:
    """Fail unless the CHILD ITSELF left evidence that it started.

    Exit status deliberately does not count: a sandbox that fails to launch also exits
    non-zero, so it cannot distinguish executed-and-denied from never-ran — the very
    ambiguity these assertions exist to remove.
    """
    assert sentinel.exists() and LIVENESS in sentinel.read_text(encoding="utf-8"), (
        f"the child never reached its write, so a denied/absent target proves nothing; {_why(proc)}"
    )


# --- enforcement (the load-bearing assertions) ------------------------------------


@live
def test_write_outside_the_allow_list_is_denied(tmp_path):
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    sentinel = allowed / "live.txt"
    target = _inside(tmp_path, "outside.txt")
    env = subprocess_env(MARK=LIVENESS, SENTINEL=str(sentinel), DENIED=str(target))
    argv = sb.wrap(
        ["/bin/sh", "-c", 'echo "$MARK" > "${SENTINEL:?}"; echo pwned > "${DENIED:?}"'],
        allow=[allowed],
        profile_dir=tmp_path,
        env=env,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    _assert_child_ran(sentinel, proc)
    assert not target.exists(), f"sandbox permitted a write outside the allow-list; {_why(proc)}"


@live
def test_write_inside_the_allow_list_succeeds(tmp_path):
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    target = allowed / "ok.txt"
    env = subprocess_env(ALLOWED=str(target))
    argv = sb.wrap(
        ["/bin/sh", "-c", 'echo ok > "${ALLOWED:?}"'],
        allow=[allowed],
        profile_dir=tmp_path,
        env=env,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    # This test needs no separate liveness check: the assertion below IS positive
    # evidence the child ran, since a child that never started writes nothing.
    # Report the sandbox's own stderr: a bare "file missing" cannot distinguish
    # "the sandbox denied it" from "the sandbox never started", and that ambiguity
    # cost a full CI round-trip to diagnose.
    assert target.exists(), f"sandbox denied a write inside the allow-list; {_why(proc)}"


@live
def test_the_incident_shape_rm_rf_is_denied(tmp_path):
    """`rm -rf <protected>/*` — the shape that expanded to `rm -rf /*` on 2026-08-26.

    The only enforcement test that removes anything, so it carries three independent
    bounds: Python confirms the target is strictly inside `tmp_path`; the path reaches
    the shell only as `"${TARGET:?}"`, so an empty value aborts instead of globbing
    from the filesystem root; and the sandbox denies the unlink. Deletion is tested
    rather than replaced by a create because unlink is a different syscall class, and
    a profile could permit it while denying writes.
    """
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    protected = _inside(tmp_path, "protected")
    protected.mkdir()
    keep = protected / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    sentinel = allowed / "live.txt"
    env = subprocess_env(MARK=LIVENESS, SENTINEL=str(sentinel), TARGET=str(protected))
    argv = sb.wrap(
        ["/bin/sh", "-c", 'echo "$MARK" > "${SENTINEL:?}"; rm -rf "${TARGET:?}"/*'],
        allow=[allowed],
        profile_dir=tmp_path,
        env=env,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    _assert_child_ran(sentinel, proc)
    assert keep.exists(), f"sandbox permitted the destructive deletion; {_why(proc)}"


@live
def test_enforcement_holds_for_the_mutmut_run_argv_shape(tmp_path):
    """The incident ran under `mutmut run`, not the baseline pytest — cover that path."""
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    sentinel = allowed / "live.txt"
    target = _inside(tmp_path, "mutmut_outside.txt")
    inner = (
        "import os, pathlib;"
        "pathlib.Path(os.environ['SENTINEL']).write_text(os.environ['MARK']);"
        "pathlib.Path(os.environ['DENIED']).write_text('pwned')"
    )
    env = subprocess_env(MARK=LIVENESS, SENTINEL=str(sentinel), DENIED=str(target))
    argv = sb.wrap([sys.executable, "-c", inner], allow=[allowed], profile_dir=tmp_path, env=env)
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    _assert_child_ran(sentinel, proc)
    assert not target.exists(), (
        f"sandbox permitted a write from the mutmut-run-shaped child; {_why(proc)}"
    )


@live
def test_the_diagnostic_rerun_allow_list_denies_writes_outside_it(tmp_path):
    """Enforcement for the `_diagnose_non_killed` path, not just its argv shape.

    That function re-runs the mutants that were NOT killed — survivors and timeouts —
    which is the set a destructive mutant lands in, so it is the call site where a
    missing sandbox costs the most. Its allow-list is `(root, basetemp)`, the same
    contract `execute_shard` uses; this proves a write outside that contract is denied
    while the child demonstrably ran. Denial is proven with a CREATE: a leaked create
    is inert, whereas proving it with a delete would perform real damage on the day the
    sandbox stopped enforcing.
    """
    root = _inside(tmp_path, "root")
    root.mkdir()
    basetemp = root / ".mutation-pytest"
    basetemp.mkdir()
    sentinel = basetemp / "live.txt"
    target = _inside(tmp_path, "outside_the_repo.txt")
    inner = (
        "import os, pathlib;"
        "pathlib.Path(os.environ['SENTINEL']).write_text(os.environ['MARK']);"
        "pathlib.Path(os.environ['DENIED']).write_text('pwned')"
    )
    env = subprocess_env(MARK=LIVENESS, SENTINEL=str(sentinel), DENIED=str(target))
    argv = sb.wrap(
        [sys.executable, "-c", inner],
        allow=(root, basetemp),
        profile_dir=tmp_path,
        env=env,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    _assert_child_ran(sentinel, proc)
    assert not target.exists(), (
        f"sandbox permitted a write outside the diagnostic re-run's allow-list; {_why(proc)}"
    )


# --- fail-closed + opt-out --------------------------------------------------------


def test_no_mechanism_raises_and_names_the_opt_out(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "probe", lambda: None)
    with pytest.raises(sb.SandboxUnavailable) as excinfo:
        sb.wrap(["/bin/true"], allow=[tmp_path], profile_dir=tmp_path, env={})
    assert sb.ALLOW_UNSANDBOXED_ENV in str(excinfo.value)
    assert sb.ALLOW_UNSANDBOXED_ENV == "REBAR_MUTATION_ALLOW_UNSANDBOXED"


def test_opt_out_waives_the_sandbox_and_warns(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sb, "probe", lambda: None)
    with caplog.at_level("WARNING"):
        argv = sb.wrap(
            ["/bin/true"],
            allow=[tmp_path],
            profile_dir=tmp_path,
            env={sb.ALLOW_UNSANDBOXED_ENV: "1"},
        )
    assert argv == ["/bin/true"], "opt-out must run the command unwrapped"
    assert any(sb.ALLOW_UNSANDBOXED_ENV in r.getMessage() for r in caplog.records)


def test_opt_out_is_off_for_falsey_values(tmp_path, monkeypatch):
    monkeypatch.setattr(sb, "probe", lambda: None)
    for value in ("", "0", "false", "no"):
        with pytest.raises(sb.SandboxUnavailable):
            sb.wrap(
                ["/bin/true"],
                allow=[tmp_path],
                profile_dir=tmp_path,
                env={sb.ALLOW_UNSANDBOXED_ENV: value},
            )


# --- mechanism selection ----------------------------------------------------------


def test_probe_prefers_seatbelt_then_bwrap_then_none(monkeypatch):
    monkeypatch.setattr(
        sb.shutil, "which", lambda n: "/usr/bin/sandbox-exec" if n == "sandbox-exec" else None
    )
    # Stub the capability probe, symmetric with `_bwrap_works` below. Without this the
    # test passes on macOS only by accident — `/usr/bin/sandbox-exec` really exists
    # there, so the real binary runs and enforces — while on Linux the path is absent,
    # `_seatbelt_works()` returns False, and `probe()` yields None.
    monkeypatch.setattr(sb, "_seatbelt_works", lambda: True)
    sb.probe.cache_clear()
    assert sb.probe() == sb.SEATBELT

    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    monkeypatch.setattr(sb, "_bwrap_works", lambda: True)
    sb.probe.cache_clear()
    assert sb.probe() == sb.BWRAP

    monkeypatch.setattr(sb.shutil, "which", lambda _n: None)
    sb.probe.cache_clear()
    assert sb.probe() is None


def test_bwrap_present_but_unable_to_namespace_is_not_offered(monkeypatch):
    """Presence on PATH is not capability.

    Ubuntu 23.10+ restricts unprivileged user namespaces via AppArmor, so an installed
    bwrap can fail with "Creating new namespace failed". Reported as available it would
    be worse than nothing — callers stop looking for a working mechanism.
    """
    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    monkeypatch.setattr(sb, "_bwrap_works", lambda: False)
    sb.probe.cache_clear()
    assert sb.probe() is None


def test_seatbelt_present_but_not_enforcing_is_not_offered(monkeypatch):
    """Presence on PATH is not capability — the same rule `bwrap` is held to.

    `sandbox-exec` is Apple-DEPRECATED. A future macOS shipping it as a non-enforcing
    stub would satisfy `shutil.which` while denying nothing, so a PATH-only probe would
    report the sandbox available and every mutation run would proceed effectively
    UNSANDBOXED — on the platform the 2026-08-26 incident actually happened on, and
    without the abort the fail-closed design promises.
    """
    monkeypatch.setattr(
        sb.shutil, "which", lambda n: "/usr/bin/sandbox-exec" if n == "sandbox-exec" else None
    )
    monkeypatch.setattr(sb, "_seatbelt_works", lambda: False)
    sb.probe.cache_clear()
    assert sb.probe() is None


def test_seatbelt_probe_observes_denial_not_just_exit_status(tmp_path, monkeypatch):
    """The probe must confirm a write was DENIED, not merely that the child ran.

    A stub `sandbox-exec` that execs its argument verbatim exits 0 while enforcing
    nothing. Keying on exit status alone would accept it.
    """
    stub = tmp_path / "sandbox-exec"
    stub.write_text(
        "#!/bin/sh\n"
        # Mimic `sandbox-exec -f <profile> <cmd>...`: drop the flag pair, run the rest.
        'while [ "$1" = "-f" ] || [ "$1" = "-p" ]; do shift 2; done\n'
        'exec "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setattr(sb.shutil, "which", lambda n: str(stub) if n == "sandbox-exec" else None)
    assert sb._seatbelt_works() is False, (
        "a non-enforcing sandbox-exec that exits 0 must NOT be reported as a mechanism"
    )


def test_seatbelt_probe_accepts_a_mechanism_that_denies_the_write(tmp_path, monkeypatch):
    """The enforcing case: the child ran AND the write did not land.

    The `@live` tests cover this with the real Seatbelt, but they skip wherever
    `probe()` finds no mechanism — every Linux cell — so without this the `return True`
    branch is exercised on no gating lane.
    """
    stub = tmp_path / "sandbox-exec"
    # Emit the liveness marker and perform no write, which is what a real deny looks
    # like from the outside.
    stub.write_text(f"#!/bin/sh\necho {sb._PROBE_MARKER}\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(sb.shutil, "which", lambda n: str(stub) if n == "sandbox-exec" else None)
    assert sb._seatbelt_works() is True


class _FixedTempDir:
    """A TemporaryDirectory stand-in yielding a chosen path (kept by the caller)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def __enter__(self) -> str:
        return str(self._path)

    def __exit__(self, *_exc: object) -> bool:
        return False


def test_seatbelt_probe_survives_a_temp_path_containing_a_space(tmp_path, monkeypatch):
    """A shell-quoting failure must not be mistaken for sandbox enforcement.

    With the redirect target unquoted, a TMPDIR containing a space makes `sh` fail the
    redirect for a reason that has nothing to do with the sandbox. The marker still
    prints and the file is still absent, so an unquoted probe reports a DENIAL that
    never happened — and a non-enforcing sandbox-exec would be accepted as working.
    """
    spaced = tmp_path / "dir with space"
    spaced.mkdir()
    monkeypatch.setattr(sb.tempfile, "TemporaryDirectory", lambda: _FixedTempDir(spaced))
    stub = tmp_path / "sandbox-exec"
    # A non-enforcing stub: it runs the command verbatim, so the write DOES land and
    # the probe must reject it.
    stub.write_text(
        '#!/bin/sh\nwhile [ "$1" = "-f" ] || [ "$1" = "-p" ]; do shift 2; done\nexec "$@"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    monkeypatch.setattr(sb.shutil, "which", lambda n: str(stub) if n == "sandbox-exec" else None)
    assert sb._seatbelt_works() is False, (
        "a non-enforcing sandbox-exec was accepted because the redirect failed on "
        "quoting rather than on sandbox policy"
    )


def test_seatbelt_probe_rejects_a_mechanism_that_never_launches(tmp_path, monkeypatch):
    """A launch failure must not read as a successful denial.

    A `sandbox-exec` that cannot start writes nothing, so the probe's target file is
    absent — indistinguishable from a real denial unless the probe also confirms the
    child RAN. Without that second signal a broken mechanism reports as working, which
    is the failure this whole module exists to prevent.
    """
    stub = tmp_path / "sandbox-exec"
    stub.write_text("#!/bin/sh\necho 'sandbox-exec: broken' >&2\nexit 1\n", encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setattr(sb.shutil, "which", lambda n: str(stub) if n == "sandbox-exec" else None)
    assert sb._seatbelt_works() is False, (
        "a sandbox-exec that never launched must NOT be reported as enforcing"
    )


def test_no_unshare_fallback_is_offered(monkeypatch):
    # `unshare --mount` denies nothing without a read-only remount, and unprivileged
    # CLONE_NEWUSER is disabled on hardened hosts — it can probe present yet not enforce.
    monkeypatch.setattr(
        sb.shutil, "which", lambda n: "/usr/bin/unshare" if n == "unshare" else None
    )
    assert sb.probe() is None


def test_bwrap_argv_makes_root_readonly_then_rebinds_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    monkeypatch.setattr(sb, "_bwrap_works", lambda: True)
    argv = sb.wrap(["/bin/true"], allow=[tmp_path], profile_dir=tmp_path)
    assert argv[0] == "bwrap"
    assert argv[1:4] == ["--ro-bind", "/", "/"]
    assert "--bind" in argv
    assert argv[argv.index("--") + 1 :] == ["/bin/true"]


# --- HOME -------------------------------------------------------------------------


def test_sandbox_env_points_home_at_a_nonexistent_path():
    out = sb.sandbox_env({"HOME": "/Users/real", "PATH": "/usr/bin"})
    assert out["HOME"] == sb.HOMELESS
    assert not Path(out["HOME"]).exists()
    assert out["PATH"] == "/usr/bin", "unrelated env must be preserved"


# The test above asserts on the MAPPING `sandbox_env` returns. That stays green even if
# nothing ever hands that mapping to a subprocess, so it cannot substantiate the claim
# that the CHILD's HOME is hardened. The two below run a real child and ask the child.


def test_a_child_launched_with_sandbox_env_observes_the_homeless_home():
    """Ask a real child what its HOME is, rather than trusting the returned dict.

    Deliberately NOT `@live`: it needs no sandbox mechanism, so it also covers the
    Linux gate cells where `probe()` returns None and every `@live` test skips.
    """
    proc = subprocess.run(
        [sys.executable, "-c", "import os; print(os.environ['HOME'])"],
        env=SubprocessEnv(sb.sandbox_env(subprocess_env())),
        capture_output=True,
        text=True,
        check=False,
    )
    # Liveness first: a child that never ran prints nothing, and an absence assertion
    # alone would read that as success.
    assert proc.returncode == 0, f"the child never ran, so its HOME proves nothing; {_why(proc)}"
    observed = proc.stdout.strip()
    assert observed == sb.HOMELESS, f"child observed HOME={observed!r}; {_why(proc)}"
    assert not Path(observed).exists(), "the hardened HOME must not exist on disk"


@live
def test_the_sandboxed_child_observes_the_homeless_home(tmp_path):
    """The same observation through `wrap()`, as `execute_shard` actually invokes it."""
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    env = SubprocessEnv(sb.sandbox_env(subprocess_env()))
    argv = sb.wrap(
        [sys.executable, "-c", "import os; print(os.environ['HOME'])"],
        allow=[allowed],
        profile_dir=tmp_path,
        env=env,
    )
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, f"the sandboxed child never ran; {_why(proc)}"
    observed = proc.stdout.strip()
    assert observed == sb.HOMELESS, f"sandboxed child observed HOME={observed!r}; {_why(proc)}"


# --- call-site binding ------------------------------------------------------------
# The tests above prove the sandbox MODULE enforces. They do not prove `execute_shard`
# actually USES it: deleting the wrap from either call site left the whole suite green.
# These assert the binding, so unwrapping a shard-test-executing subprocess fails here.


def _execute_shard_fn() -> ast.FunctionDef:
    tree = ast.parse((REPO_ROOT / "scripts" / "mutation_gate.py").read_text(encoding="utf-8"))
    return next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "execute_shard"
    )


def _execute_shard_run_calls() -> list[ast.Call]:
    return [
        n
        for n in ast.walk(_execute_shard_fn())
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "_run"
    ]


def _is_sandbox_wrapped(call: ast.Call) -> bool:
    if not call.args:
        return False
    first = call.args[0]
    return (
        isinstance(first, ast.Call)
        and isinstance(first.func, ast.Attribute)
        and first.func.attr == "wrap"
        and isinstance(first.func.value, ast.Name)
        and first.func.value.id == "mutation_sandbox"
    )


def _argv_literals(call: ast.Call) -> tuple[str, ...]:
    node = call.args[0]
    inner = node.args[0] if isinstance(node, ast.Call) and node.args else node
    if not isinstance(inner, ast.Tuple):
        return ()
    return tuple(
        e.value for e in inner.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
    )


def test_the_mutmut_run_subprocess_is_sandbox_wrapped():
    """The 2026-08-26 incident ran under `mutmut run`; unwrapping it must fail here."""
    calls = [
        c for c in _execute_shard_run_calls() if _argv_literals(c)[:3] == ("-m", "mutmut", "run")
    ]
    assert len(calls) == 1, "expected exactly one `mutmut run` invocation in execute_shard"
    assert _is_sandbox_wrapped(calls[0]), (
        "the `mutmut run` subprocess is NOT sandbox-wrapped — this is the exact path "
        "that expanded to `rm -rf /*` on 2026-08-26"
    )


def _inner_name(call: ast.Call) -> str | None:
    """The argv variable name passed to _run, seen through the sandbox wrapper."""
    if not call.args:
        return None
    node = call.args[0]
    inner = node.args[0] if isinstance(node, ast.Call) and node.args else node
    return inner.id if isinstance(inner, ast.Name) else None


def test_the_baseline_pytest_subprocess_is_sandbox_wrapped():
    # The baseline argv is built as the `clean_args` tuple variable above the call,
    # so it is matched by name rather than by inline string literals.
    calls = [c for c in _execute_shard_run_calls() if _inner_name(c) == "clean_args"]
    assert len(calls) == 1, "expected exactly one baseline pytest invocation in execute_shard"
    assert _is_sandbox_wrapped(calls[0]), "the baseline pytest subprocess is NOT sandbox-wrapped"


def test_reporting_subprocesses_are_not_wrapped():
    """`mutmut results` / `show` execute no shard code; wrapping them is out of scope."""
    reporting = [
        c
        for c in _execute_shard_run_calls()
        if _argv_literals(c)[:3] in {("-m", "mutmut", "results"), ("-m", "mutmut", "show")}
    ]
    assert reporting, "expected the reporting invocations to still exist"
    assert not any(_is_sandbox_wrapped(c) for c in reporting)


# --- profile construction (LLM-Review 2256, advisory) ------------------------------
# build_seatbelt_profile was exercised only through the @live tests, which run on no
# gating lane. These assert its output directly and are platform-independent.


def test_profile_denies_writes_then_permits_the_allow_list(tmp_path):
    profile = sb.build_seatbelt_profile([tmp_path])
    assert "(deny file-write*)" in profile
    assert profile.index("(deny file-write*)") < profile.index("(allow file-write*")
    assert f'(subpath "{tmp_path.resolve()}")' in profile


def test_profile_permits_only_narrow_device_paths(tmp_path):
    profile = sb.build_seatbelt_profile([tmp_path])
    for literal in ("/dev/null", "/dev/stdout", "/dev/stderr"):
        assert f'(literal "{literal}")' in profile
    assert '(subpath "/dev")' not in profile, "must not make all of /dev writable"


def test_profile_escapes_quotes_and_backslashes_in_paths(tmp_path):
    nasty = tmp_path / 'we"ird\\path'
    nasty.mkdir(parents=True, exist_ok=True)
    profile = sb.build_seatbelt_profile([nasty])
    body = profile.split("(allow file-write*", 1)[1]
    # Every double-quote inside the subpath literal must be escaped, or the
    # S-expression terminates early and the write-deny is silently malformed.
    subpath_line = next(ln for ln in body.splitlines() if "subpath" in ln)
    inner = subpath_line.strip()[len('(subpath "') : -len('")')]
    assert '\\"' in inner or '"' not in inner


def test_sandbox_env_suppresses_bytecode_writes():
    # The venv is not writable, so a __pycache__ write would fail the run.
    assert sb.sandbox_env({})["PYTHONDONTWRITEBYTECODE"] == "1"


def test_bwrap_skips_allow_paths_that_do_not_exist(tmp_path, monkeypatch):
    # bwrap --bind aborts when SRC is missing; skipping keeps deny-by-default intact.
    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    monkeypatch.setattr(sb, "_bwrap_works", lambda: True)
    missing = tmp_path / "not-created-yet"
    argv = sb.wrap(["/bin/true"], allow=[tmp_path, missing], profile_dir=tmp_path)
    assert str(tmp_path.resolve()) in argv
    assert str(missing.resolve()) not in argv


def test_bwrap_uses_minimal_dev_not_a_writable_host_bind(tmp_path, monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    monkeypatch.setattr(sb, "_bwrap_works", lambda: True)
    argv = sb.wrap(["/bin/true"], allow=[tmp_path], profile_dir=tmp_path)
    assert "--dev" in argv and "--dev-bind" not in argv


# --- CI is the disposable environment (LLM-Review 2269) ---------------------------
# GitHub runners ship bubblewrap but deny unprivileged user namespaces, so the
# functional probe correctly finds no sandbox and the gate would fail closed on every
# CI run. A runner IS the isolated environment the sandbox substitutes for. A
# workstation is not — and that is where the 2026-08-26 rm -rf /* landed.


def test_ci_runner_proceeds_unsandboxed_with_a_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(sb, "probe", lambda: None)
    with caplog.at_level("WARNING"):
        argv = sb.wrap(["/bin/true"], allow=[tmp_path], profile_dir=tmp_path, env={"CI": "true"})
    assert argv == ["/bin/true"], "CI must proceed rather than abort"
    assert any("UNSANDBOXED" in r.getMessage() for r in caplog.records)


def test_workstation_still_aborts_when_no_sandbox(tmp_path, monkeypatch):
    # The load-bearing half: without CI set, the abort path is unchanged.
    monkeypatch.setattr(sb, "probe", lambda: None)
    with pytest.raises(sb.SandboxUnavailable):
        sb.wrap(["/bin/true"], allow=[tmp_path], profile_dir=tmp_path, env={})


@pytest.mark.parametrize("value", ["", "0", "false", "no"])
def test_falsey_ci_values_do_not_waive_the_sandbox(tmp_path, monkeypatch, value):
    monkeypatch.setattr(sb, "probe", lambda: None)
    with pytest.raises(sb.SandboxUnavailable):
        sb.wrap(["/bin/true"], allow=[tmp_path], profile_dir=tmp_path, env={"CI": value})


def test_a_working_sandbox_is_used_even_on_ci(tmp_path, monkeypatch):
    # CI is a fallback for the ABSENCE of a mechanism, never a reason to skip one.
    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    monkeypatch.setattr(sb, "_bwrap_works", lambda: True)
    sb.probe.cache_clear()
    argv = sb.wrap(["/bin/true"], allow=[tmp_path], profile_dir=tmp_path, env={"CI": "true"})
    assert argv[0] == "bwrap", "a usable sandbox must be used regardless of CI"


def _sandbox_env_target(fn: ast.FunctionDef) -> str:
    """The variable `execute_shard` assigns `mutation_sandbox.sandbox_env(...)` to."""
    targets = [
        node.targets[0].id
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "sandbox_env"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "mutation_sandbox"
    ]
    assert len(targets) == 1, (
        "expected exactly one `env = mutation_sandbox.sandbox_env(...)` in execute_shard; "
        f"found {len(targets)}. Without it the child inherits the real HOME and a mutant's "
        "writes land in the developer's home directory."
    )
    return targets[0]


def test_the_sandboxed_subprocesses_receive_the_hardened_env():
    """The hardened env must actually REACH both children, not merely be computed.

    Deleting `env = mutation_sandbox.sandbox_env(env)` from execute_shard left the whole
    suite green (28 sandbox + 42 gate tests), because the only HOME test asserted on the
    mapping the function returns rather than on any call site. This binds the two
    sandbox-wrapped `_run` calls to that hardened env, so removing or bypassing it fails
    here — the same control the wrap-binding tests above provide for `wrap` itself.
    """
    name = _sandbox_env_target(_execute_shard_fn())
    wrapped = [c for c in _execute_shard_run_calls() if _is_sandbox_wrapped(c)]
    assert len(wrapped) == 2, f"expected 2 sandbox-wrapped _run calls, found {len(wrapped)}"
    for call in wrapped:
        env_kw = next((k.value for k in call.keywords if k.arg == "env"), None)
        assert isinstance(env_kw, ast.Name) and env_kw.id == name, (
            "a sandbox-wrapped subprocess does not receive the hardened env "
            f"({name!r}); its child would inherit the real HOME"
        )


# --- the guards themselves ---------------------------------------------------------
# These run everywhere (no sandbox needed), so the bounds on the destructive test are
# themselves verified on every lane rather than assumed.


def test_the_shell_guard_aborts_before_the_destructive_line():
    """`"${VAR:?}"` must abort the script BEFORE the line that would delete.

    Deliberately contains NO destructive command. A test that proved this by really
    running `rm -rf "$EMPTY"/*` would itself perform the incident this module exists to
    prevent (`e668-b496`) whenever the guard stopped working. Asserting that the shell
    never REACHES the marker gives the same evidence with no blast radius.
    """
    proc = subprocess.run(
        ["/bin/sh", "-c", 'echo start; : "${TARGET:?}"; echo reached-the-danger-point'],
        env=subprocess_env(TARGET=""),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode != 0, f"an empty TARGET must abort the shell; {_why(proc)}"
    assert "start" in proc.stdout, f"the shell should have run up to the guard; {_why(proc)}"
    assert "reached-the-danger-point" not in proc.stdout, (
        'the guard did not stop the script — a real `rm -rf "$TARGET"/*` on that '
        "line would have expanded from the filesystem root"
    )


def test_the_containment_guard_refuses_a_path_outside_tmp(tmp_path):
    """`_inside` must refuse to hand `rm -rf` anything outside the pytest tmp dir."""
    with pytest.raises(AssertionError, match="not strictly inside"):
        _inside(tmp_path, "../escape")


def test_the_containment_guard_refuses_an_empty_name(tmp_path):
    """An empty name would resolve to tmp_path itself, widening the removal."""
    with pytest.raises(AssertionError, match="empty target name"):
        _inside(tmp_path, "")


# --- module-wide binding: no `mutmut run` may be added unsandboxed --------------------
# The tests above bind the two call sites inside `execute_shard`. That scoping is what
# let a THIRD `mutmut run` — `_diagnose_non_killed`, which re-runs surviving mutants —
# sit unsandboxed on main (`724c-b5fd`): a per-function assertion cannot see a call site
# in another function. These scan the whole module, so a fourth site fails here rather
# than silently reopening the hole.


def _module_tree() -> ast.Module:
    return ast.parse((REPO_ROOT / "scripts" / "mutation_gate.py").read_text(encoding="utf-8"))


def _is_mutmut_run(node: ast.AST) -> bool:
    """A `_run(...)` whose argv literals begin `-m mutmut run` (starred args ignored)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run"
        and _argv_literals(node)[:3] == ("-m", "mutmut", "run")
    )


def _mutmut_run_sites() -> list[tuple[str, ast.Call]]:
    """Every `mutmut run` invocation in mutation_gate.py, with its enclosing function."""
    seen: dict[int, tuple[str, ast.Call]] = {}
    for fn in (n for n in ast.walk(_module_tree()) if isinstance(n, ast.FunctionDef)):
        for call in ast.walk(fn):
            if _is_mutmut_run(call):
                seen.setdefault(id(call), (fn.name, call))
    return list(seen.values())


def test_every_mutmut_run_in_the_module_is_sandbox_wrapped():
    """Mutated code must not execute unsandboxed from ANY call site."""
    sites = _mutmut_run_sites()
    assert len(sites) >= 2, (
        f"expected at least the execute_shard and _diagnose_non_killed sites, found "
        f"{[name for name, _ in sites]}"
    )
    unwrapped = [name for name, call in sites if not _is_sandbox_wrapped(call)]
    assert not unwrapped, (
        f"`mutmut run` executes MUTATED code unsandboxed in {unwrapped} — this is the "
        f"2026-08-26 path"
    )


def test_every_mutmut_run_in_the_module_receives_the_hardened_env():
    """Each site must also get the hardened HOME, not just the sandbox wrapper."""
    tree = _module_tree()
    offenders = []
    for name, call in _mutmut_run_sites():
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        hardened = _sandbox_env_target(fn)
        env_kw = next((k.value for k in call.keywords if k.arg == "env"), None)
        if not (isinstance(env_kw, ast.Name) and env_kw.id == hardened):
            offenders.append(name)
    assert not offenders, (
        f"a `mutmut run` child inherits the real HOME in {offenders}; adjacent "
        f"home-directory writes would land in the developer's home"
    )


# --- observability: the conditions a reader needs are IN the log ---------------------


def test_a_missing_allow_path_is_surfaced_not_silently_dropped(tmp_path, monkeypatch, caplog):
    """A skipped bind must name itself, or its absence is indistinguishable from intent.

    bwrap aborts if a --bind source is missing, so the wrapper skips it. Silently, a
    caller that forgot to create a directory sees only a confusing write failure from
    INSIDE the sandbox, with nothing pointing at the cause.
    """
    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    monkeypatch.setattr(sb, "_bwrap_works", lambda: True)
    missing = tmp_path / "never-created"
    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        argv = sb.wrap(["/bin/true"], allow=[missing], profile_dir=tmp_path)
    assert str(missing) in caplog.text, (
        f"the skipped allow-list path was not named in the log; got {caplog.text!r}"
    )
    assert str(missing) not in argv, "a non-existent path must not be handed to --bind"


def test_the_ci_fail_open_warning_names_the_variable_and_its_value(monkeypatch, caplog):
    """A run that proceeds UNSANDBOXED must be unmistakable in its own log.

    Ambient `CI` remains the sole condition on this fall-back (tightening that is
    `f11d-f8fd`), so the log is the only place a reader can tell that a run had no
    sandbox at all. It must name the variable AND the value that permitted it.
    """
    monkeypatch.setattr(sb, "probe", lambda: None)
    env = {"CI": "true"}
    with caplog.at_level(logging.WARNING, logger=sb.logger.name):
        argv = sb.wrap(["/bin/true"], allow=[], profile_dir=Path("."), env=env)
    assert argv == ["/bin/true"], "the CI fall-back returns the command unwrapped"
    assert "UNSANDBOXED" in caplog.text
    assert "CI=" in caplog.text and "true" in caplog.text, (
        f"the warning must name the variable and its observed value; got {caplog.text!r}"
    )
