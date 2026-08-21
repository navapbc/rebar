"""One implementation of "spawn a detached rebar child" (task 2dc4-9bcd-75b9-4544).

rebar detaches children that outlive the command that started them — the async
tickets-branch push, the enrichment drain, the compaction sweep, and the snapshot-GC
trigger — and each site used to
carry its own copy of the PYTHONPATH bootstrap, the ``-c`` re-entry stub, the platform detach
flags and the stdio discipline. A defect (the missing durable ``cwd``, bug
3198-438c-72a5-470f) propagated to all three by exactly that imitation. These tests pin the
consolidation: the pattern's signature exists only in ``rebar._proc.spawn_detached``, its
construction is correct, and its boundaries (per-caller env, per-caller catch) hold.

The end-to-end cwd contract across the original three call sites stays pinned by
``tests/unit/test_detached_child_cwd.py``, which drives the real sites with real processes.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

from rebar import _proc

_SRC_REBAR = Path(_proc.__file__).resolve().parent
_SRC = _SRC_REBAR.parent

# The tokens that CONSTITUTE a detached-rebar-child spawn, as they actually appear in the
# code: the Windows detach flags, and the bare-python re-entry bootstrap the -c stub uses.
# `start_new_session` alone is deliberately NOT scanned — three other spawn sites
# (grounding/harness.py, adapters/jira/acli_subprocess.py, adapters/jira_family/
# wiki_render.py) legitimately use it for process-group reaping, not rebar re-entry.
_SIGNATURE_TOKENS = (
    "DETACHED_PROCESS",
    "CREATE_NO_WINDOW",
    "sys.path.insert(0, sys.argv",
)


def test_the_detached_spawn_signature_appears_only_in_proc() -> None:
    """A STATIC scan, not a runtime assertion: a fourth copy-pasted site that no test happens
    to execute must still fail here. This is what makes the consolidation durable — the class
    (one omission, replicated by imitation) cannot re-enter by copy-paste."""
    offenders: list[str] = []
    for path in sorted(_SRC_REBAR.rglob("*.py")):
        if path == Path(_proc.__file__).resolve():
            continue
        text = path.read_text(encoding="utf-8")
        for token in _SIGNATURE_TOKENS:
            if token in text:
                offenders.append(f"{path.relative_to(_SRC)}: {token}")
    assert offenders == [], (
        "the detached-rebar-child spawn signature leaked outside _proc.py — route the new "
        f"site through rebar._proc.spawn_detached instead: {offenders}"
    )


def test_proc_remains_a_stdlib_only_leaf() -> None:
    """The leaf posture is a stated invariant of the module (its docstring) and of the ticket:
    both the in-process library and the path-loaded reconciler subprocess import it, so a
    ``rebar.*`` import here would form a cycle."""
    text = Path(_proc.__file__).read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import rebar", "from rebar")):
            pytest.fail(f"_proc.py gained a rebar.* import, breaking the leaf posture: {line!r}")


class _CapturedSpawn:
    def __init__(self) -> None:
        self.argv: list[str] | None = None
        self.kwargs: dict | None = None


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> _CapturedSpawn:
    """Capture the Popen call by patching the module's OWN `subprocess` reference — never the
    real module, which fixture teardown still needs."""
    cap = _CapturedSpawn()

    def _fake_popen(argv: list[str], **kwargs: object) -> object:
        cap.argv, cap.kwargs = argv, kwargs
        return object()

    monkeypatch.setattr(
        _proc,
        "subprocess",
        types.SimpleNamespace(Popen=_fake_popen, DEVNULL=subprocess.DEVNULL),
    )
    return cap


def test_spawn_detached_builds_the_bare_python_reentry(
    tmp_path: Path, captured: _CapturedSpawn
) -> None:
    """The whole shared construction at once: interpreter, re-entry stub, argv, PYTHONPATH
    bootstrap, stdio discipline, detach flags, and the durable cwd anchored on the ARGUMENT
    (not on whoever spawned it)."""
    tracker = tmp_path / "repo" / ".tickets-tracker"
    tracker.mkdir(parents=True)

    _proc.spawn_detached(
        "rebar.llm.enrich_drain",
        "drain",
        str(tracker),
        env={"HOME": "/home/x"},
        stderr=subprocess.DEVNULL,
    )

    assert captured.argv is not None and captured.kwargs is not None
    assert captured.argv[0] == sys.executable
    assert captured.argv[1] == "-c"
    assert "sys.path.insert(0, sys.argv[2])" in captured.argv[2]
    assert (
        "import rebar.llm.enrich_drain; rebar.llm.enrich_drain.drain(*sys.argv[1:2])"
        in (captured.argv[2])
    )
    assert captured.argv[3] == str(tracker)
    assert captured.argv[4] == str(_SRC)
    assert captured.kwargs["cwd"] == _proc.detached_child_cwd(str(tracker))
    assert captured.kwargs["env"]["PYTHONPATH"].startswith(str(_SRC))
    assert captured.kwargs["stdin"] == subprocess.DEVNULL
    assert captured.kwargs["stdout"] == subprocess.DEVNULL
    assert captured.kwargs["stderr"] == subprocess.DEVNULL
    assert captured.kwargs["start_new_session"] is True  # POSIX CI
    assert captured.kwargs["close_fds"] is True


def test_spawn_detached_hands_every_argument_to_the_entry_point(
    tmp_path: Path, captured: _CapturedSpawn
) -> None:
    """A multi-argument entry point (the snapshot-GC trigger passes the store root AND a
    repo-root sentinel): every argument rides argv in order, the bootstrap src slot follows
    them, and the cwd anchor derives from the FIRST argument."""
    store = tmp_path / "gate-snapshots"
    store.mkdir()

    _proc.spawn_detached(
        "rebar._snapshot.gc_trigger",
        "run_detached",
        str(store),
        "",
        env={},
        stderr=subprocess.DEVNULL,
    )

    assert captured.argv is not None and captured.kwargs is not None
    assert "sys.path.insert(0, sys.argv[3])" in captured.argv[2]
    assert "gc_trigger.run_detached(*sys.argv[1:3])" in captured.argv[2]
    assert captured.argv[3] == str(store)
    assert captured.argv[4] == ""
    assert captured.argv[5] == str(_SRC)
    assert captured.kwargs["cwd"] == _proc.detached_child_cwd(str(store))


def test_spawn_detached_requires_at_least_one_argument() -> None:
    """The cwd anchor and the re-entry stub both key on the first argument, so a zero-arg
    spawn is a caller bug surfaced loudly, not a child that dies quietly later."""
    with pytest.raises(ValueError, match="at least one argument"):
        _proc.spawn_detached("rebar.x", "f", env={}, stderr=subprocess.DEVNULL)


def test_spawn_detached_layers_pythonpath_without_mutating_the_callers_env(
    tmp_path: Path, captured: _CapturedSpawn
) -> None:
    """Env construction is PER-CALLER by design (the push passes a secret-stripped
    projection), so the helper must only layer PYTHONPATH onto a COPY: mutating the caller's
    dict would let one spawn contaminate the caller's later use of it."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    caller_env = {"PYTHONPATH": "/caller/libs", "KEEP": "1"}

    _proc.spawn_detached(
        "rebar._store.push",
        "push_tickets_branch",
        str(tracker),
        env=caller_env,
        stderr=subprocess.DEVNULL,
    )

    assert caller_env == {"PYTHONPATH": "/caller/libs", "KEEP": "1"}, "caller env was mutated"
    assert captured.kwargs is not None
    assert captured.kwargs["env"]["PYTHONPATH"] == str(_SRC) + os.pathsep + "/caller/libs"
    assert captured.kwargs["env"]["KEEP"] == "1"


def test_spawn_detached_reraises_so_each_caller_keeps_its_own_catch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure posture stays per-caller: the push catches only ``OSError``, the drain and
    sweep catch broad ``Exception``. The helper therefore RE-RAISES — owning the catch here
    would silently broaden the push's exposure."""
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()

    def _boom(argv: list[str], **kwargs: object) -> object:
        raise OSError("no such executable")

    monkeypatch.setattr(
        _proc,
        "subprocess",
        types.SimpleNamespace(Popen=_boom, DEVNULL=subprocess.DEVNULL),
    )

    with pytest.raises(OSError, match="no such executable"):
        _proc.spawn_detached(
            "rebar._store.push",
            "push_tickets_branch",
            str(tracker),
            env={},
            stderr=subprocess.DEVNULL,
        )


def test_windows_detach_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Migrated from test_enrich_drain.py when the per-site ``_detach_kwargs`` collapsed into
    ``_proc``: the API-derisked Windows branch keeps its assertion at the one remaining home."""
    monkeypatch.setattr(_proc.os, "name", "nt")
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x8000000, raising=False)
    kw = _proc._detach_kwargs()
    assert "creationflags" in kw
    assert "start_new_session" not in kw
