"""Oracle for bounded gate concurrency with fast-fail admission (story 09da-343c-1ee9-480c).

ADR 0112 decision 5. Every assertion here is on OBSERVABLE behaviour — a gate is admitted or
refused, the refusal names congestion and is not a verdict, a slot comes back — never on how
the counter is represented. The cross-process cases spawn a real child because a bound that
covers only the in-process MCP daemon path and not separate-process CLI runs is not a bound.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rebar.llm import gate_admission as ga

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]

# Bounded so a leaked child cannot outlive the suite: it exits on its own well inside the
# per-test timeout even if the test that spawned it dies first.
_CHILD_HOLD_SECONDS = 20

_CHILD = """
import os, sys, time
sys.path.insert(0, {src!r})
os.environ["REBAR_GATE_TMPDIR"] = {tmpdir!r}
from rebar.llm import gate_admission as ga
d = ga.slot_dir()
held = [ga.acquire_slot({limit}, d) for _ in range({limit})]
assert all(fd is not None for fd in held), held
print("READY", flush=True)
time.sleep({hold})
"""


@pytest.fixture
def gate_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An isolated 'host': its own snapshot store and its own ``[snapshot]`` config.

    ``limit(n)`` writes the operator-facing config key, so the tests drive the cap the way
    an operator would rather than by patching the resolver.
    """
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(tmp_path))
    # A real user-level rebar config on the developer's box would otherwise leak into the
    # merged [snapshot] table and change the cap under the test.
    from rebar import _config_sources

    monkeypatch.setattr(_config_sources, "user_config_path", lambda: tmp_path / "absent.toml")

    def limit(n: int) -> Path:
        (tmp_path / "rebar.toml").write_text(f"[snapshot]\nmax_concurrent_gates = {n}\n")
        return tmp_path

    return limit


def _spawn_holder(tmp_path: Path, limit: int) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _CHILD.format(
                src=str(REPO_ROOT / "src"),
                tmpdir=str(tmp_path),
                limit=limit,
                hold=_CHILD_HOLD_SECONDS,
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "READY", "holder process never took its slots"
    return proc


# ── AC1: one shared counter bounds both gates ────────────────────────────────────────


def test_admission_below_the_cap_runs_the_gate(gate_host):
    root = gate_host(2)
    ran = []
    with ga.gate_admission("plan_review", "t-1", root):
        ran.append(True)
    assert ran == [True]


def test_admission_at_the_cap_is_refused_while_the_holders_run(gate_host):
    root = gate_host(2)
    with ga.gate_admission("plan_review", "t-1", root):
        with ga.gate_admission("plan_review", "t-2", root):
            with pytest.raises(ga.GateCongestedError):
                with ga.gate_admission("plan_review", "t-3", root):
                    pytest.fail("a third gate was admitted above a cap of 2")


def test_plan_review_and_completion_verifier_share_one_counter(gate_host):
    """Two caps of N each would admit 2N holders, which is the bound that was needed."""
    root = gate_host(1)
    with ga.gate_admission("plan_review", "t-1", root):
        with pytest.raises(ga.GateCongestedError):
            with ga.gate_admission("verify_completion", "t-2", root):
                pytest.fail("verify_completion was admitted against plan_review's slot")


def test_a_freed_slot_is_admitted_again(gate_host):
    root = gate_host(1)
    with ga.gate_admission("plan_review", "t-1", root):
        pass
    with ga.gate_admission("verify_completion", "t-2", root):
        pass  # no raise: the counter is not monotonic


# ── AC2: the refusal is immediate, not a queue ───────────────────────────────────────


def test_the_refusal_returns_immediately_rather_than_waiting_for_a_holder(gate_host):
    """A queued gate would still hold its thread and its ~739 MB resident while waiting."""
    root = gate_host(1)
    with ga.gate_admission("plan_review", "t-1", root):
        started = time.monotonic()
        with pytest.raises(ga.GateCongestedError):
            with ga.gate_admission("plan_review", "t-2", root):
                pass
        elapsed = time.monotonic() - started
    # The refusal is a non-blocking lock attempt costing microseconds; the only alternative
    # behaviour (a BLOCKING acquire, i.e. the queue this story rejects) never returns at all
    # while the holder above is still inside its `with`. The ceiling therefore dwarfs the
    # expected wall time by ~six orders of magnitude and can only fire on that hang.
    # timing: hang-guard — a queueing acquire would never return; 5 s vs ~microseconds expected
    assert elapsed < 5.0, f"admission blocked for {elapsed:.2f}s instead of fast-failing"


# ── AC3: the refusal is actionable and is NOT a verdict ──────────────────────────────


def test_the_refusal_names_congestion_and_tells_the_caller_to_retry(gate_host):
    root = gate_host(1)
    with ga.gate_admission("plan_review", "t-1", root):
        with pytest.raises(ga.GateCongestedError) as excinfo:
            with ga.gate_admission("plan_review", "t-2", root):
                pass
    message = str(excinfo.value).lower()
    assert "congestion" in message
    assert "retry" in message
    assert "no verdict was produced" in message


def test_the_refusal_is_not_a_gate_verdict(gate_host):
    """It must not be readable as INDETERMINATE or as a failed review."""
    root = gate_host(1)
    with ga.gate_admission("plan_review", "t-1", root):
        with pytest.raises(ga.GateCongestedError) as excinfo:
            with ga.gate_admission("plan_review", "t-2", root):
                pass
    exc = excinfo.value
    assert not isinstance(exc, dict)
    for verdict_word in ("INDETERMINATE", "BLOCK", "FAIL", "PASS"):
        assert verdict_word not in str(exc)


def test_the_mcp_review_plan_surface_returns_a_retryable_refusal_not_a_verdict(monkeypatch):
    import rebar.llm
    from rebar import _mcp_llm

    def congested(*args, **kwargs):
        raise ga.GateCongestedError("plan_review", 4)

    monkeypatch.setattr(rebar.llm, "review_plan", congested)
    result = _mcp_llm._review_plan_body("t-1", None, None, False, readonly=True)
    assert result["retryable"] is True
    assert result["error"] == "gate_congested"
    assert "verdict" not in result
    assert "congestion" in result["message"].lower()


def test_the_mcp_verify_completion_surface_returns_a_retryable_refusal(monkeypatch):
    import rebar.llm
    from rebar import _mcp_llm

    def congested(*args, **kwargs):
        raise ga.GateCongestedError("verify_completion", 4)

    monkeypatch.setattr(rebar.llm, "verify_completion", congested)
    result = _mcp_llm._verify_completion_body("t-1", None, None, None, readonly=True)
    assert result["retryable"] is True
    assert result["error"] == "gate_congested"
    assert "verdict" not in result


def test_the_cli_reports_congestion_as_a_transient_retry_exit_code(monkeypatch, capsys):
    """Exit 11 — the code this repo already documents as 'transient — retry', and distinct
    from every verdict code (0 pass, 1 fail, 2 indeterminate, 12 not-current)."""
    from rebar import llm
    from rebar._cli import _llm_commands

    def congested(*args, **kwargs):
        raise ga.GateCongestedError("plan_review", 4)

    monkeypatch.setattr(llm, "review_plan", congested)
    monkeypatch.setattr(_llm_commands, "ensure_initialized", lambda **kw: None, raising=False)
    code = _llm_commands._review_plan(["09da-343c-1ee9-480c"])
    assert code == 11
    assert "congestion" in capsys.readouterr().err.lower()


# ── AC4: the bound spans processes ───────────────────────────────────────────────────


@pytest.mark.allow_unharnessed_subprocess(
    "a bound that covers only the in-process MCP daemon and not separate-process CLI runs "
    "is not a bound; only a real second process can prove the counter is shared"
)
def test_a_separate_process_holding_the_slots_congests_this_one(gate_host, tmp_path):
    root = gate_host(2)
    holder = _spawn_holder(tmp_path, 2)
    try:
        with pytest.raises(ga.GateCongestedError):
            with ga.gate_admission("plan_review", "t-1", root):
                pytest.fail("admitted while another process held every slot")
    finally:
        holder.kill()
        holder.wait(timeout=10)


# ── AC5: releasing is crash-safe ─────────────────────────────────────────────────────


@pytest.mark.allow_unharnessed_subprocess(
    "crash-safety is only demonstrable by killing a real holder process"
)
def test_a_killed_holder_does_not_permanently_consume_a_slot(gate_host, tmp_path):
    root = gate_host(1)
    holder = _spawn_holder(tmp_path, 1)
    with pytest.raises(ga.GateCongestedError):
        with ga.gate_admission("plan_review", "t-1", root):
            pytest.fail("admitted while the holder was alive")
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)
    deadline = time.monotonic() + 10
    while True:
        try:
            with ga.gate_admission("plan_review", "t-2", root):
                return  # reclaimed: the kernel dropped the dead holder's lock
        except ga.GateCongestedError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_a_gate_that_raises_still_gives_its_slot_back(gate_host):
    """A leak on the error path degrades the cap into a deadlock only a reboot clears."""
    root = gate_host(1)
    with pytest.raises(ValueError):
        with ga.gate_admission("plan_review", "t-1", root):
            raise ValueError("the gate blew up")
    with ga.gate_admission("plan_review", "t-2", root):
        pass


# ── AC6: the cap is a config key with a measured default ─────────────────────────────


def test_the_default_cap_is_four(gate_host, tmp_path):
    """Sized from measurement: 4 x ~748 MB peak plan-review RSS ~= 2.99 GB, inside the ~3 GiB
    a t4g.large leaves after ~2.17 GB steady state and Gerrit's ~3 GiB reservation."""
    assert ga.DEFAULT_MAX_CONCURRENT_GATES == 4
    assert ga.max_concurrent_gates(tmp_path) == 4


def test_the_cap_is_read_from_the_snapshot_config_table(gate_host):
    root = gate_host(7)
    assert ga.max_concurrent_gates(root) == 7


def test_zero_disables_the_bound_entirely(gate_host):
    root = gate_host(0)
    with ga.gate_admission("plan_review", "t-1", root):
        with ga.gate_admission("plan_review", "t-2", root):
            with ga.gate_admission("plan_review", "t-3", root):
                pass  # no refusal: 0 is the operator's documented off switch


def test_a_malformed_cap_falls_back_to_the_default(gate_host, tmp_path):
    (tmp_path / "rebar.toml").write_text('[snapshot]\nmax_concurrent_gates = "many"\n')
    assert ga.max_concurrent_gates(tmp_path) == ga.DEFAULT_MAX_CONCURRENT_GATES


def test_a_negative_cap_falls_back_to_the_default(gate_host, tmp_path):
    (tmp_path / "rebar.toml").write_text("[snapshot]\nmax_concurrent_gates = -3\n")
    assert ga.max_concurrent_gates(tmp_path) == ga.DEFAULT_MAX_CONCURRENT_GATES


# ── AC7: congestion is visible, not silent ───────────────────────────────────────────


def test_a_rejected_admission_emits_the_congestion_marker(gate_host, capsys):
    root = gate_host(1)
    with ga.gate_admission("plan_review", "t-1", root):
        with pytest.raises(ga.GateCongestedError):
            with ga.gate_admission("plan_review", "t-2", root):
                pass
    err = capsys.readouterr().err
    marker_lines = [line for line in err.splitlines() if line.startswith(ga.MARKER + " {")]
    assert len(marker_lines) == 1, err
    import json

    body = json.loads(marker_lines[0][len(ga.MARKER) + 1 :])
    assert body["gate"] == "plan_review"
    assert body["ticket_id"] == "t-2"
    assert body["limit"] == 1


def test_an_admitted_gate_emits_no_congestion_marker(gate_host, capsys):
    root = gate_host(2)
    with ga.gate_admission("plan_review", "t-1", root):
        pass
    assert ga.MARKER not in capsys.readouterr().err


# ── the wiring: both entry points are actually wrapped ───────────────────────────────


def test_review_plan_refuses_before_resolving_a_snapshot_when_congested(gate_host, monkeypatch):
    """Admission must precede snapshot materialization — the bytes are spent there."""
    root = gate_host(1)
    from rebar.llm import gate_source, plan_review

    def fail_resolve(*args, **kwargs):
        raise AssertionError("a snapshot was resolved for a gate that was never admitted")

    monkeypatch.setattr(gate_source, "resolve_gate_handle", fail_resolve)
    with ga.gate_admission("plan_review", "holder", root):
        monkeypatch.chdir(root)
        with pytest.raises(ga.GateCongestedError):
            plan_review.review_plan("t-1", repo_root=str(root))


def test_verify_completion_refuses_before_resolving_a_snapshot_when_congested(
    gate_host, monkeypatch
):
    root = gate_host(1)
    from rebar.llm import completion, gate_source

    def fail_resolve(*args, **kwargs):
        raise AssertionError("a snapshot was resolved for a gate that was never admitted")

    monkeypatch.setattr(gate_source, "resolve_gate_handle", fail_resolve)
    monkeypatch.setattr(completion, "_pinned_ticket_view_selection", lambda _r: (False, "eager"))
    with ga.gate_admission("verify_completion", "holder", root):
        monkeypatch.chdir(root)
        with pytest.raises(ga.GateCongestedError):
            completion.verify_completion("t-1", repo_root=str(root))


# ── AC8: a disarmed cap says so ──────────────────────────────────────────────────────


def _disarm_markers(err: str) -> list[str]:
    return [ln for ln in err.splitlines() if ln.startswith(ga.DISARMED_MARKER + " {")]


def test_an_unusable_slot_file_admits_and_announces_the_disarm(gate_host, monkeypatch, capsys):
    """The store root is reachable but a slot file is not: a local fault, so failing open is
    right — bricking every gate over it would be worse — but it is never silent."""
    root = gate_host(1)

    def unusable(_path):
        raise PermissionError("slot file is not writable")

    monkeypatch.setattr(ga, "_open_slot", unusable)
    admitted = []
    with ga.gate_admission("plan_review", "t-1", root):
        admitted.append(True)
    with ga.gate_admission("plan_review", "t-2", root):
        admitted.append(True)  # and the bound really is off, not merely bypassed once
    assert admitted == [True, True]
    lines = _disarm_markers(capsys.readouterr().err)
    assert len(lines) == 2, lines
    import json

    body = json.loads(lines[0][len(ga.DISARMED_MARKER) + 1 :])
    assert body["gate"] == "plan_review"
    assert "unusable" in body["reason"]


def test_an_unreachable_scratch_store_refuses_rather_than_admitting(gate_host, monkeypatch):
    """ADR 0112: admission must treat an unmounted scratch volume as a refusal, never as an
    empty cache to repopulate onto the root filesystem."""
    root = gate_host(1)

    def unmounted() -> Path:
        raise OSError("scratch volume is not mounted")

    monkeypatch.setattr(ga, "slot_dir", unmounted)
    with pytest.raises(ga.GateScratchUnavailableError) as excinfo:
        with ga.gate_admission("plan_review", "t-1", root):
            pytest.fail("a gate ran with its scratch volume unreachable")
    message = str(excinfo.value).lower()
    assert "unreachable" in message
    assert "retry" in message


def test_the_scratch_refusal_is_retryable_over_mcp(monkeypatch):
    import rebar.llm
    from rebar import _mcp_llm

    def unavailable(*args, **kwargs):
        raise ga.GateScratchUnavailableError("plan_review", "OSError: not mounted")

    monkeypatch.setattr(rebar.llm, "review_plan", unavailable)
    result = _mcp_llm._review_plan_body("t-1", None, None, False, readonly=True)
    assert result["retryable"] is True
    assert result["error"] == "gate_scratch_unavailable"
    assert "verdict" not in result


def test_a_platform_without_flock_admits_and_announces_the_disarm(gate_host, monkeypatch, capsys):
    root = gate_host(1)
    monkeypatch.setattr(ga, "fcntl", None)
    with ga.gate_admission("plan_review", "t-1", root):
        pass
    lines = _disarm_markers(capsys.readouterr().err)
    assert len(lines) == 1, lines
    assert "fcntl" in lines[0]


def test_the_operator_off_switch_is_not_reported_as_a_disarm(gate_host, capsys):
    """`0` is a choice, not a fault; marking it would train operators to ignore the marker."""
    root = gate_host(0)
    with ga.gate_admission("plan_review", "t-1", root):
        pass
    assert _disarm_markers(capsys.readouterr().err) == []
