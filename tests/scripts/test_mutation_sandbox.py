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
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


sb = _load("mutation_sandbox")

live = pytest.mark.skipif(
    sb.probe() is None,
    reason="no OS sandbox mechanism on this host (sandbox-exec or bwrap)",
)


# --- enforcement (the load-bearing assertions) ------------------------------------


@live
def test_write_outside_the_allow_list_is_denied(tmp_path):
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    target = tmp_path / "outside.txt"
    argv = sb.wrap(
        ["/bin/sh", "-c", f"echo pwned > {target}"], allow=[allowed], profile_dir=tmp_path
    )
    subprocess.run(argv, capture_output=True, text=True, check=False)
    assert not target.exists(), "sandbox permitted a write outside the allow-list"


@live
def test_write_inside_the_allow_list_succeeds(tmp_path):
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    target = allowed / "ok.txt"
    argv = sb.wrap(["/bin/sh", "-c", f"echo ok > {target}"], allow=[allowed], profile_dir=tmp_path)
    subprocess.run(argv, capture_output=True, text=True, check=False)
    assert target.exists(), "sandbox denied a write inside the allow-list"


@live
def test_the_incident_shape_rm_rf_is_denied(tmp_path):
    """`rm -rf <protected>/*` — verbatim what expanded to `rm -rf /*` on 2026-08-26."""
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    keep = protected / "keep.txt"
    keep.write_text("keep", encoding="utf-8")
    argv = sb.wrap(
        ["/bin/sh", "-c", f"rm -rf {protected}/*"], allow=[allowed], profile_dir=tmp_path
    )
    subprocess.run(argv, capture_output=True, text=True, check=False)
    assert keep.exists(), "sandbox permitted the destructive deletion"


@live
def test_enforcement_holds_for_the_mutmut_run_argv_shape(tmp_path):
    """The incident ran under `mutmut run`, not the baseline pytest — cover that path."""
    allowed = tmp_path / "scratch"
    allowed.mkdir()
    target = tmp_path / "mutmut_outside.txt"
    inner = f"import subprocess; subprocess.run(['/bin/sh','-c','echo x > {target}'])"
    argv = sb.wrap(
        [sys.executable, "-c", inner], allow=[allowed, Path(sys.prefix)], profile_dir=tmp_path
    )
    subprocess.run(argv, capture_output=True, text=True, check=False)
    assert not target.exists(), "sandbox permitted a write from the mutmut-run-shaped child"


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
    assert sb.probe() == sb.SEATBELT
    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
    assert sb.probe() == sb.BWRAP
    monkeypatch.setattr(sb.shutil, "which", lambda _n: None)
    assert sb.probe() is None


def test_no_unshare_fallback_is_offered(monkeypatch):
    # `unshare --mount` denies nothing without a read-only remount, and unprivileged
    # CLONE_NEWUSER is disabled on hardened hosts — it can probe present yet not enforce.
    monkeypatch.setattr(
        sb.shutil, "which", lambda n: "/usr/bin/unshare" if n == "unshare" else None
    )
    assert sb.probe() is None


def test_bwrap_argv_makes_root_readonly_then_rebinds_allowed(tmp_path, monkeypatch):
    monkeypatch.setattr(sb.shutil, "which", lambda n: "/usr/bin/bwrap" if n == "bwrap" else None)
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


# --- call-site binding ------------------------------------------------------------
# The tests above prove the sandbox MODULE enforces. They do not prove `execute_shard`
# actually USES it: deleting the wrap from either call site left the whole suite green.
# These assert the binding, so unwrapping a shard-test-executing subprocess fails here.


def _execute_shard_run_calls() -> list[ast.Call]:
    tree = ast.parse((REPO_ROOT / "scripts" / "mutation_gate.py").read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "execute_shard"
    )
    return [
        n
        for n in ast.walk(fn)
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
