"""SIGTERM ends a detached compaction sweep at the next ticket boundary (bug topnotch-brazen-slug).

On 2026-10-18 a retiring MCP container's detached sweep kept the shared tickets clone's write
lock through a blue-green overlap, because ``docker stop`` signals only PID 1 and the sweep
runs in its own session. The retiring server now forwards SIGTERM to the sweep; the sweep must
let the fold in flight finish and then stop before the next ticket. The child below runs the
real ``_run_sweep`` loop with a stubbed fold, and is bounded by ``communicate(timeout=...)``
and killed in ``finally``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(os.name == "nt", reason="POSIX signal delivery"),
]

_CHILD_SRC = """
import sys, time
from rebar._commands import compact as c
from rebar._commands import compact_trigger as t
from rebar._logging import install_stderr_handler

install_stderr_handler()  # run_sweep installs the same default-level handler
t.install_stop_on_sigterm()

def fold(tracker, tid, repo_root, position_commits, *, no_commit=False):
    print("start " + tid, flush=True)
    time.sleep(1.5 if tid == "t2" else 0.05)
    print("end " + tid, flush=True)
    return "."

c._sweep_one_ticket = fold
c._sweep_position_map = lambda _repo_root: None
c._pause = lambda _s: None
compacted, _skipped, _ids, _errors = c._run_sweep(sys.argv[1], ["t1", "t2", "t3", "t4"], None)
print("done %d" % compacted, flush=True)
"""


def test_sigterm_mid_fold_finishes_that_fold_and_stops_before_the_next(tmp_path: Path) -> None:
    tracker = tmp_path / "store"
    tracker.mkdir()
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SRC, str(tracker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        seen: list[str] = []
        # The sweep loop also writes its one-character progress marks to stdout, so a fold's
        # "start" line can carry a leading ".".
        for line in child.stdout:
            seen.append(line.strip())
            if line.strip().endswith("start t2"):
                break
        assert seen and seen[-1].endswith("start t2"), f"never reached ticket 2: {seen}"
        child.send_signal(signal.SIGTERM)
        out, err = child.communicate(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()

    text = "\n".join(seen) + "\n" + out
    assert child.returncode == 0, f"SIGTERM killed the sweep (rc={child.returncode}): {err}"
    assert "end t2" in text, f"the in-flight fold did not finish: {text}"
    assert "start t3" not in text, f"the sweep started another ticket after SIGTERM: {text}"
    assert "done 2" in text, text
    assert "SIGTERM" in err, f"the stop was not logged at the default level: {err!r}"
