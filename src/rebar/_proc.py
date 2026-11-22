"""Share process-group reaping and detached-child spawning across rebar.

Grounding and ACLI callers supply their own grace, drain, and log identity.
Async push and background workers share a durable cwd and import bootstrap.
This stdlib-only leaf remains importable by path-loaded reconciler code.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
from typing import IO


def reap_process_group(
    proc: subprocess.Popen[str],
    *,
    grace: float,
    drain: float,
    label: str,
    logger: logging.Logger,
) -> None:
    """Reap a timed-out process group and pipe-holding descendants.

    On POSIX, signal TERM, allow ``grace``, then KILL and bound the final ``drain``;
    log survivors with the caller's label. Handle exited-group races. Other
    platforms kill the child, end its descendants (:func:`_kill_windows_descendants`)
    and wait within the supplied bounds.
    """
    if os.name != "posix":
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        _kill_windows_descendants(proc.pid, label=label, logger=logger)
        try:
            proc.wait(timeout=grace + drain)
        except subprocess.TimeoutExpired:
            logger.warning("%s child PID %s did not exit after kill (leaked)", label, proc.pid)
        return

    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        # Child already gone (ESRCH) or we can't see it — best-effort reap and return.
        try:
            proc.wait(timeout=drain)
        except subprocess.TimeoutExpired:
            pass
        return

    # SIGTERM the group, then give it a grace window to flush + exit cleanly.
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=grace)
        return  # exited on SIGTERM within the grace window — drained.
    except subprocess.TimeoutExpired:
        pass

    # Grace expired — SIGKILL the group, then bound the final reap/drain so a
    # D-state child cannot hang us indefinitely.
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.communicate(timeout=drain)
    except subprocess.TimeoutExpired:
        logger.warning(
            "%s process group %s survived SIGKILL after %ss drain (leaked PID %s)",
            label,
            pgid,
            drain,
            proc.pid,
        )


# ── Windows: descendants of a timed-out child ────────────────────────────────────────
# Windows has no process groups to signal, so ``proc.kill()`` (TerminateProcess) ends only the
# direct child and a grandchild that inherited its stdout keeps the pipe open (bug
# aerial-flameproof-antbear). Windows never re-parents a process, and the caller's un-waited
# ``Popen`` keeps the child's handle open so its pid cannot be reused; a parent-pid walk from
# that pid therefore finds every descendant, including one whose own parent already exited
# (the case ``taskkill /T``, which starts from a live pid, cannot reach).
_TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_TERMINATE = 0x0001
_INVALID_HANDLE_VALUE = -1
#: Walk passes: the second catches a process a descendant started while the first ran.
_DESCENDANT_PASSES = 2


def _windows_process_table() -> list[tuple[int, int]]:
    """``(pid, parent_pid)`` for every process, from one Toolhelp snapshot. Windows only."""
    import ctypes
    from ctypes import wintypes

    class _Entry(ctypes.Structure):  # PROCESSENTRY32W
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry))
    k32.Process32FirstW.restype = wintypes.BOOL
    k32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(_Entry))
    k32.Process32NextW.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL
    snap = k32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if not snap or snap == wintypes.HANDLE(_INVALID_HANDLE_VALUE).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")  # type: ignore[attr-defined]
    table: list[tuple[int, int]] = []
    try:
        entry = _Entry()
        entry.dwSize = ctypes.sizeof(_Entry)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            table.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return table


def _windows_terminate(pid: int) -> None:
    """``TerminateProcess`` one pid. Windows only; raises ``OSError`` when it cannot."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.CloseHandle.restype = wintypes.BOOL
    handle = k32.OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        raise OSError(ctypes.get_last_error(), f"OpenProcess({pid}) failed")  # type: ignore[attr-defined]
    try:
        if not k32.TerminateProcess(handle, 1):
            raise OSError(ctypes.get_last_error(), f"TerminateProcess({pid}) failed")  # type: ignore[attr-defined]
    finally:
        k32.CloseHandle(handle)


def _descendants(root: int, table: list[tuple[int, int]]) -> list[int]:
    """Every pid whose parent chain in *table* reaches *root*, deepest first.

    Deepest first means a grandchild is ended before its parent could notice and respawn
    it. *root* itself is excluded; a self-parented entry (pid 0) never loops."""
    children: dict[int, list[int]] = {}
    for pid, parent in table:
        if pid != parent:
            children.setdefault(parent, []).append(pid)
    order: list[int] = []
    seen = {root}
    frontier = [root]
    while frontier:
        nxt: list[int] = []
        for parent in frontier:
            for pid in children.get(parent, ()):
                if pid not in seen:
                    seen.add(pid)
                    order.append(pid)
                    nxt.append(pid)
        frontier = nxt
    return order[::-1]


def _kill_windows_descendants(root: int, *, label: str, logger: logging.Logger) -> None:
    """Terminate every descendant of *root*. Best-effort: a failed snapshot or terminate is
    logged at debug and the caller's reap continues exactly as it would without it."""
    for _ in range(_DESCENDANT_PASSES):
        try:
            found = _descendants(root, _windows_process_table())
        except Exception as exc:  # noqa: BLE001 — no kernel32, snapshot refused: reap as before
            logger.debug("%s: cannot list descendants of PID %s: %s", label, root, exc)
            return
        if not found:
            return
        for pid in found:
            try:
                _windows_terminate(pid)
            except OSError as exc:  # already gone, or not ours to end
                logger.debug("%s: cannot end descendant PID %s: %s", label, pid, exc)


def store_repo_root(tracker: str | os.PathLike[str]) -> str:
    """Resolve the store repo root from the tracker's real path.

    Symlink resolution avoids ephemeral worktree parents. This is the store root,
    not the code/config root; callers needing ``rebar.toml`` resolve that separately.
    A relative path from a removed cwd may raise ``OSError``.
    """
    # repo-root-seam: THE sanctioned store-repo-root derivation; every other site routes here.
    return os.path.dirname(os.path.realpath(tracker))


def detached_child_cwd(tracker: str) -> str:
    """Anchor a detached child outside its possibly short-lived caller worktree.

    Use the canonical store repo root, then its nearest existing ancestor; never
    the tracker itself. A dead cwd or other path failure falls back to filesystem
    root without failing the triggering write.
    """
    try:
        root = store_repo_root(tracker)
    except OSError:
        return os.sep
    while root and not os.path.isdir(root):
        parent = os.path.dirname(root)
        if parent == root:
            break
        root = parent
    return root if root and os.path.isdir(root) else os.sep


def _detach_kwargs() -> dict:
    """Platform detach kwargs. POSIX: a new session so the child outlives the parent. Windows:
    DETACHED_PROCESS | CREATE_NO_WINDOW (constants exist only on Windows, referenced only
    inside this branch), used by every :func:`spawn_detached` caller there (the async push,
    the compaction sweep and snapshot GC)."""
    if os.name == "nt":  # pragma: no cover - POSIX CI
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,  # type: ignore[attr-defined]
        }
    return {"start_new_session": True, "close_fds": True}


def spawn_detached(
    module: str,
    func: str,
    *args: str,
    env: dict[str, str],
    stderr: int | IO[str],
) -> None:
    """Spawn a detached Python child with durable cwd and import path.

    Copy the caller's env before adding PYTHONPATH; the caller supplies stderr and
    handles any ``Popen`` failure. ``args[0]`` must be a canonical store path for
    the cwd anchor. Arguments remain strings on the child's argv.
    """
    if not args:
        raise ValueError("spawn_detached needs at least one argument for the child")
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    child_env = dict(env)
    child_env["PYTHONPATH"] = src + (
        os.pathsep + child_env["PYTHONPATH"] if child_env.get("PYTHONPATH") else ""
    )
    n = len(args)
    subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"import sys; sys.path.insert(0, sys.argv[{n + 1}]); "
            f"import {module}; {module}.{func}(*sys.argv[1:{n + 1}])",
            *args,
            src,
        ],
        cwd=detached_child_cwd(args[0]),
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=stderr,
        **_detach_kwargs(),
    )
