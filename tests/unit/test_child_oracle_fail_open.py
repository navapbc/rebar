"""Absent-string child oracles must not fail OPEN on a signal-killed child.

Bug 1241-b83c-f8c7-40bf. Sibling of f0fb-de7a-b315-4508 (``tests/e2e``) and
0e1d-c698-c38d-4c3e (the live-DC tier), which fixed the same construct class elsewhere.

A test whose verdict is *the absence of a string in a child's output* is only as trustworthy
as its assumption that the child ran. A child killed by a signal is torn down by the kernel
before it writes, so ``stdout == stderr == ""``, the bad string is trivially absent, and the
oracle reports GREEN for a run that never executed. That is the dangerous direction: a
spuriously red test costs an hour, a spuriously green one destroys the evidence someone is
relying on it to produce.

WHY THESE TESTS DRIVE THE REAL TEST BODIES rather than the shared helper. The helper
(``tests/_child_diag.py``) already has its own coverage in ``test_live_dc_pass_health.py``.
A helper test cannot see a *call site that forgot to call it* — and the defect here is
exactly an absent call, at five independent sites. So each test below forces the real
site's real child to die on a signal, through that site's own seam, and invokes the shipped
test function. Both sides of the assertion therefore come from different places: the kill is
arranged by this file, the verdict is produced by the site under test.

WHAT IS COVERED, AND WHAT IS NOT. These cover a child **terminated by a signal**, which
CPython surfaces as a NEGATIVE ``returncode`` (``-signal.SIGKILL``). SIGKILL is used because
it is the one signal a process cannot trap, so the empty output is guaranteed rather than
incidental; the guard keys on the SIGN of the returncode, so any signal behaves the same.

A positive ``128 + N`` (e.g. 137) is deliberately NOT rejected, and the reason is measured
rather than assumed. None of the five sites uses ``shell=True``; each execs the real program
directly (``bash``, ``python``, ``bash``, ``pandoc``, ``make``), so killing the process Python
spawned yields ``-9`` at ALL FIVE. ``128 + N`` arises only when a GRANDCHILD dies and the
direct child survives to report it -- and a survivor writes its own diagnostic, so the capture
is NOT empty. Measured on each site's real spawn path:

    site1  bash killed            rc=-9    output_empty=True
    site1  grandchild killed      rc=-9    output_empty=True    (bash execs into it)
    site3  bash killed            rc=-9    output_empty=True
    site3  grandchild killed      rc=137   output_empty=False
    site5  make killed            rc=-9    output_empty=True
    site5  recipe child killed    rc=2     output_empty=False
    site2/4 direct child killed   rc=-9    output_empty=True    (no grandchild at all)

EMPTY output and a POSITIVE returncode never co-occur. That is what makes the boundary safe:
the fail-open needs an empty capture, and an empty capture at these sites only ever comes with
a negative returncode. When the code IS positive the oracle has real output to inspect, so it
is doing its job -- and rejecting 137 there would be a new claim about which exit codes each
site may legitimately produce, which is a widening rather than a restoration.

Also not covered: a child that exits normally with empty output for some other reason. The
sites that can pin a specific exit code do so; the ones that cannot only assert the child was
not killed.
"""

from __future__ import annotations

import importlib.util
import os
import signal
import subprocess
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A shell that SIGKILLs itself. Writes nothing first -- the empty output IS the defect.
_SUICIDE_SH = "#!/bin/sh\nkill -9 $$\n"

#: The same, as a bare ``bash -c`` body for the sites that pass a script string around.
_SUICIDE_BODY = "kill -9 $$"


def _suicide_shim(directory: Path, name: str) -> Path:
    """An executable named *name* in *directory* that dies on SIGKILL. Returns *directory*."""
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / name
    shim.write_text(_SUICIDE_SH, encoding="utf-8")
    shim.chmod(0o755)
    return shim


def _load(module_name: str, relative_path: str) -> types.ModuleType:
    """Import a test module by PATH under a private name.

    ``importlib`` rather than a plain import: several of these basenames (``test_cli``)
    collide with modules already in ``sys.modules`` from other tiers, and a private name
    keeps this file from perturbing whatever else the session has imported.
    """
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    assert spec is not None and spec.loader is not None, relative_path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _assert_names_the_signal(excinfo: pytest.ExceptionInfo[AssertionError], what: str) -> None:
    """The failure must say a signal killed the child, not merely that something failed.

    A bare "the check failed" sends the reader hunting for a product regression. Naming
    SIGKILL tells them an external process reaped it -- the reason f0fb-de7a-b315-4508
    built ``child_failure_detail`` in the first place.
    """
    message = str(excinfo.value)
    assert "SIGKILL" in message, f"the failure does not name the signal; got: {message!r}"
    assert what in message, f"the failure does not say WHAT did not complete; got: {message!r}"


def test_the_suicide_shim_really_produces_a_signal_killed_child(tmp_path: Path) -> None:
    """Negative control: prove the fixture, so a green below cannot be a broken kill.

    Every test in this file rests on the claim that its child really died on a signal and
    really wrote nothing. Assert that claim once, directly, instead of trusting it five
    times implicitly.
    """
    shim = _suicide_shim(tmp_path / "bin", "victim")

    proc = subprocess.run([str(shim)], capture_output=True, text=True, check=False)

    assert proc.returncode == -signal.SIGKILL, f"fixture: expected -9, got {proc.returncode}"
    assert proc.stdout == "" and proc.stderr == "", (
        f"fixture: a signal-killed child writes nothing; got {proc.stdout!r} / {proc.stderr!r}"
    )


# ── Site 1 — the Git-floor gate skip oracle ──────────────────────────────────


def test_git_floor_gate_skip_oracle_rejects_a_signal_killed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``::error::`` absent from EMPTY output must not read as a clean skip.

    Seam: the gate step's script body, which the site lifts out of the workflow YAML and
    runs under ``bash -c``. Replacing it with a self-killing body leaves the site's own
    ``_run_gate``, its own fixture and its own assertion entirely real.
    """
    module = _load("_failopen_git_floor", "tests/unit/test_git_floor_gate_tree_skew.py")
    monkeypatch.setattr(module, "_gate_script", lambda: _SUICIDE_BODY)
    tree = tmp_path / "predates-the-gate"
    (tree / ".github").mkdir(parents=True)

    with pytest.raises(AssertionError) as excinfo:
        module.test_absent_floor_file_emits_no_workflow_error(tree)

    _assert_names_the_signal(excinfo, "the Git-floor gate")


# ── Site 2 — the reconcile routing oracle ────────────────────────────────────


def test_reconcile_routing_oracle_rejects_a_signal_killed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The "unknown subcommand" text, absent from EMPTY output must not read as correct routing.

    Seam: ``sys.executable``, which the site interpolates into ``[sys.executable, "-m",
    "rebar.cli", ...]``. Pointing it at a self-killing shim keeps the spawn, the argv
    assembly and the assertion the site's own -- nothing about ``subprocess`` is faked.
    """
    module = _load("_failopen_cli", "tests/interfaces/facades/test_cli.py")
    monkeypatch.setattr(module.sys, "executable", str(_suicide_shim(tmp_path / "bin", "python")))
    repo = tmp_path / "repo"
    repo.mkdir()

    with pytest.raises(AssertionError) as excinfo:
        module.test_top_level_reconcile_route_is_removed_before_operational_work(
            repo, {"PATH": os.environ["PATH"]}
        )

    _assert_names_the_signal(excinfo, "the removed reconcile route check")


# ── Site 3 — the re-dispatch "no warning" oracle ─────────────────────────────


def test_redispatch_no_warning_oracle_rejects_a_signal_killed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``returncode != 0`` is satisfied by ``-9``, so the absent ``::warning::`` fails open.

    This is the site the ticket flags as borderline: it DOES consult the returncode, but
    ``!= 0`` accepts a signal kill, and its remaining claim is an absence. Seam: the
    workflow step's script body.
    """
    module = _load("_failopen_redispatch", "tests/unit/workflow/test_reconcile_workflow_lint.py")
    monkeypatch.setattr(module, "_redispatch_script", lambda: _SUICIDE_BODY)

    with pytest.raises(AssertionError) as excinfo:
        module.test_redispatch_requires_both_the_422_and_the_disabled_workflow_signal(tmp_path)

    _assert_names_the_signal(excinfo, "the re-dispatch step")


# ── Site 4 — the pandoc colour oracle (Step-7 construct sibling) ─────────────


def test_pandoc_colour_oracle_rejects_a_signal_killed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``{color:`` absent from EMPTY stdout must not read as "pandoc emits no colour".

    Found by the Step-7 construct sweep, not named on the ticket: same shape, same tier.
    Seam: ``wiki_render._pandoc_path``, the site's own resolution of the binary to spawn.
    """
    module = _load("_failopen_wiki", "tests/unit/rebar_reconciler/test_wiki_render.py")
    shim = str(_suicide_shim(tmp_path / "bin", "pandoc"))
    monkeypatch.setattr(module.wiki_render, "_pandoc_path", lambda: shim)

    with pytest.raises(AssertionError) as excinfo:
        module.test_pandoc_emits_no_color_form()

    _assert_names_the_signal(excinfo, "pandoc")


# ── Site 5 — the actionlint fail-fast oracle (Step-7 construct sibling) ──────


def test_actionlint_fail_fast_oracle_rejects_a_signal_killed_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every claim in that test -- ``!= 0``, no binary, no "installed" -- a signal kill meets.

    Found by the Step-7 construct sweep. It is the purest example of the class: three
    assertions, all of them satisfiable by a child that never ran. Seam: the minimal PATH
    the site resolves ``make`` from.
    """
    module = _load("_failopen_actionlint", "tests/unit/test_ci_actionlint_install.py")
    bin_dir = tmp_path / "bin"
    _suicide_shim(bin_dir, "make")
    monkeypatch.setattr(module, "_SANE_PATH", str(bin_dir))

    with pytest.raises(AssertionError) as excinfo:
        module.test_actionlint_install_fails_fast_on_download_failure(tmp_path / "work")

    _assert_names_the_signal(excinfo, "the actionlint-bin recipe")


# ── the invariant the guard's sufficiency rests on ───────────────────────────
#
# The guard above rejects only a NEGATIVE returncode. That is sufficient ONLY BECAUSE an
# empty capture and a positive returncode never co-occur at these sites: the fail-open needs
# an empty capture, and whatever survives to report a positive `128 + N` writes a diagnostic
# first. That is a load-bearing premise, not a background fact -- if it stops holding, the
# guard silently narrows and the fail-open returns while the docstring above still says it was
# measured. So it is asserted, not merely recorded.
#
# Scoped to the three sites where it is NON-OBVIOUS -- the ones whose child has grandchildren
# of its own (bash runs commands; make runs `sh`/`curl`), so a signal could in principle
# surface as `137` from a surviving intermediate. Sites 2 and 4 are excluded deliberately:
# they exec the working process directly with nothing in between, so a negative returncode is
# CPython's documented `subprocess` behaviour and asserting it would be testing the standard
# library.
#
# DETERMINISM: every kill here is SELF-inflicted (`kill -9 $$` inside the child). There is no
# external killer to race, no sleep, and no wall-clock assertion -- the child cannot be killed
# before it exists, because it is the thing doing the killing. A test that kills a process it
# spawned is exactly how a flaky test gets written; this shape has no timing component at all.


def _site1_killed_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Drive site 1's own ``_run_gate`` with a gate script that SIGKILLs itself."""
    module = _load("_inv_git_floor", "tests/unit/test_git_floor_gate_tree_skew.py")
    monkeypatch.setattr(module, "_gate_script", lambda: _SUICIDE_BODY)
    tree = tmp_path / "predates-the-gate"
    (tree / ".github").mkdir(parents=True)
    return module._run_gate(tree)


def _site3_killed_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Drive site 3's own ``_run_redispatch`` with a step script that SIGKILLs itself."""
    module = _load("_inv_redispatch", "tests/unit/workflow/test_reconcile_workflow_lint.py")
    monkeypatch.setattr(module, "_redispatch_script", lambda: _SUICIDE_BODY)
    work = tmp_path / "redispatch"
    work.mkdir(parents=True)
    return module._run_redispatch(work, "exit 0")


def _site5_killed_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Drive site 5's REAL spawn, capturing the process it produced.

    Site 5 is the one site whose ``subprocess.run`` is inline in the test body rather than
    factored into a helper, so there is no seam that hands back the ``CompletedProcess``. A
    pass-through recorder around the module's ``subprocess.run`` observes the real spawn --
    real argv, real env, real ``make`` resolution -- without changing what runs.
    """
    module = _load("_inv_actionlint", "tests/unit/test_ci_actionlint_install.py")
    _suicide_shim(tmp_path / "bin", "make")
    monkeypatch.setattr(module, "_SANE_PATH", str(tmp_path / "bin"))
    seen: list[subprocess.CompletedProcess[str]] = []
    real_run = subprocess.run

    def _recording(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        proc = real_run(*args, **kwargs)  # type: ignore[arg-type,call-overload]
        seen.append(proc)
        return proc

    monkeypatch.setattr(module.subprocess, "run", _recording)
    with pytest.raises(AssertionError):
        module.test_actionlint_install_fails_fast_on_download_failure(tmp_path / "work")
    assert seen, "the site did not spawn anything; the recorder saw no subprocess"
    return seen[-1]


@pytest.mark.parametrize(
    "spawn",
    [
        pytest.param(_site1_killed_child, id="site1-git-floor-gate"),
        pytest.param(_site3_killed_child, id="site3-redispatch-step"),
        pytest.param(_site5_killed_child, id="site5-actionlint-recipe"),
    ],
)
def test_an_empty_capture_only_ever_comes_with_a_negative_returncode(
    spawn, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A signal kill through a site WITH grandchildren still surfaces as a negative code.

    If this ever fails with a positive returncode and an empty capture, the guard at that
    site no longer covers its own hazard -- the fail-open is back, and the fix's sufficiency
    argument is void.
    """
    proc = spawn(tmp_path, monkeypatch)

    assert proc.returncode < 0, (
        "a signal kill through this site's spawn path surfaced as a POSITIVE returncode "
        f"({proc.returncode}). assert_child_was_not_signal_killed only rejects negatives, so "
        "this site is no longer covered by it."
    )
    assert (proc.stdout or "") + (proc.stderr or "") == "", (
        "fixture precondition: the killed child was expected to write nothing; got "
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )
