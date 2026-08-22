"""ab54: a PASS/FAIL COMPLETION_VERDICT sidecar must not be SILENTLY lost on write-lock
contention.

THE DEFECT. ``completion_sidecar.emit`` calls ``append_event``, which can raise
``LockTimeout`` (a TRANSIENT, retryable write-lock contention — 60s budget). ``emit`` caught
ALL exceptions, logged a swallowed ``logger.warning`` and returned ``False``; the caller
``close_precheck`` discarded that bool. Net effect: under contention the durable record
vanished with exit 0, clean stdout, no stderr — a transient failure handled identically to a
permanent one, and invisibly.

THE FIX (asserted here):
- a ``LockTimeout`` on the sidecar write is RETRIED at least once, so a transient contention
  that clears LANDS the record (both at the raw seam boundary and at the real lock boundary,
  where ``append_event`` wraps ``LockTimeout`` into a ``CommandError``);
- when the sidecar is ULTIMATELY dropped, the close writes an EXPLICIT ``Warning: ... WITHOUT
  ...`` stderr line naming the ticket, and the close still SUCCEEDS + stays certified
  (best-effort policy preserved);
- ``close_precheck`` inspects ``emit``'s bool at BOTH the PASS and FAIL sites.

Assertions are on OBSERVABLE behaviour (the landed sidecar record, the captured stderr, the
returned sign signal), never on a private name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._store.lock import LockTimeout

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.setenv("REBAR_SIGNING_KEY", "k")
    rebar.init_repo(repo_root=str(repo))
    return repo


def _pass_verdict(tid: str) -> dict:
    return {
        "verdict": "PASS",
        "ticket_id": tid,
        "findings": [],
        "criteria": [{"criterion": "AC1", "met": True, "kind": "codebase-verifiable"}],
        "certifiable": True,
        "runner": "fake",
    }


# ── AC1 + AC4: a raw-seam LockTimeout is retried and the record LANDS ────────────────────────
def test_pass_sidecar_retries_raw_lock_timeout_and_lands(store, monkeypatch):
    """Inject a ``LockTimeout`` on the FIRST ``append_event`` call (the seam boundary), then
    delegate to the real committer on the retry. The PASS record must LAND and ``emit`` must
    report success.

    RED on the pre-fix code: ``emit`` does not retry, so the first ``LockTimeout`` is swallowed,
    ``emit`` returns ``False`` and ``latest_pass_record`` stays ``None``.
    """
    from rebar._commands import _seam
    from rebar.llm import completion_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "retry work", description="x" * 60, repo_root=r)

    real_append = _seam.append_event
    calls = {"n": 0}

    def flaky_append(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LockTimeout(60)
        return real_append(*a, **k)

    monkeypatch.setattr(_seam, "append_event", flaky_append)

    ok = completion_sidecar.emit(_pass_verdict(tid), repo_root=r)

    assert calls["n"] >= 2, "emit must RE-ATTEMPT the write after a LockTimeout (it did not)"
    assert ok is True, "emit must report success once the retry lands the record"
    landed = completion_sidecar.latest_pass_record(tid, repo_root=r)
    assert landed is not None, "the PASS sidecar record was LOST despite a retryable LockTimeout"
    assert (
        landed.get("ticket_id")
        == rebar._engine_support.resolver.resolve_ticket_id(tid, str(rebar.config.tracker_dir(r)))
        or landed.get("ticket_id") == tid
    )


# ── AC1 + AC4: a real lock-boundary LockTimeout (wrapped by append_event) is retried ─────────
def test_pass_sidecar_retries_lock_boundary_timeout_and_lands(store, monkeypatch):
    """FAITHFUL injection at the LOCK boundary: ``write_and_push`` (what the real store calls
    while holding the write lock) raises ``LockTimeout`` on the first attempt. Here the real
    ``append_event`` WRAPS that ``LockTimeout`` into a ``CommandError`` — the retry must still
    fire and the record must land.

    RED on the pre-fix code: no retry ⇒ record lost, ``emit`` returns ``False``.
    """
    from rebar._store import event_append
    from rebar.llm import completion_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "boundary work", description="x" * 60, repo_root=r)

    real_wap = event_append.write_and_push
    calls = {"n": 0}

    def flaky_wap(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise LockTimeout(60)
        return real_wap(*a, **k)

    monkeypatch.setattr(event_append, "write_and_push", flaky_wap)

    ok = completion_sidecar.emit(_pass_verdict(tid), repo_root=r)

    assert calls["n"] >= 2, "emit must re-attempt after a lock-boundary LockTimeout"
    assert ok is True
    assert completion_sidecar.latest_pass_record(tid, repo_root=r) is not None, (
        "the PASS record was lost on a retryable lock-boundary timeout"
    )


# ── AC5: a PERMANENT LockTimeout leaves emit best-effort (False), record absent ──────────────
def test_pass_sidecar_permanent_lock_timeout_returns_false(store, monkeypatch):
    from rebar._store import event_append
    from rebar.llm import completion_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "permafail work", description="x" * 60, repo_root=r)

    def always_timeout(*a, **k):
        raise LockTimeout(60)

    monkeypatch.setattr(event_append, "write_and_push", always_timeout)

    ok = completion_sidecar.emit(_pass_verdict(tid), repo_root=r)

    assert ok is False, "a permanent LockTimeout must return False (best-effort, never raise)"
    assert completion_sidecar.latest_pass_record(tid, repo_root=r) is None


def _drive_precheck_pass(store, monkeypatch, tid: str):
    """Run the real ``_completion_precheck`` down its PASS emit branch for ``tid``."""
    from rebar._commands import close_precheck
    from rebar._commands import gates as _gates

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)

    import rebar.llm as _llm

    monkeypatch.setattr(
        _llm, "verify_completion", lambda *a, **k: _pass_verdict(tid), raising=False
    )
    result, _expectation = close_precheck._completion_precheck(
        tid,
        "task",
        str(store),
        str(store),
        reason="",
        force_close="",
    )
    return result


# ── AC2 + AC3 + AC5: a DROPPED PASS sidecar warns on stderr; the close still proceeds ────────
def test_close_precheck_pass_warns_when_sidecar_dropped(store, monkeypatch, capsys):
    """When ``emit`` ultimately returns ``False`` on a PASS close, ``close_precheck`` must write
    an explicit ``Warning: ... WITHOUT ...`` stderr line naming the ticket, AND still return the
    sign signal (the close succeeds + stays certified).

    RED on the pre-fix code: the bool is discarded, so nothing is written to stderr.
    """
    from rebar.llm import completion_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "drop pass", description="x" * 60, repo_root=r)

    monkeypatch.setattr(completion_sidecar, "emit", lambda *a, **k: False)

    out = _drive_precheck_pass(store, monkeypatch, tid)

    assert out is not None, "the PASS close must still return its sign signal (best-effort policy)"
    assert str(out.get("verdict", "")).upper() == "PASS"
    err = capsys.readouterr().err
    assert "Warning" in err and "WITHOUT" in err, (
        "a dropped PASS sidecar must surface an explicit stderr warning"
    )
    assert tid.split("-")[0] in err or tid in err, "the warning must name the ticket"


# ── AC2 + AC3: a DROPPED FAIL sidecar warns on stderr; the FAIL still blocks the close ───────
def test_close_precheck_fail_warns_when_sidecar_dropped(store, monkeypatch, capsys):
    from rebar._commands import close_precheck
    from rebar._commands import gates as _gates
    from rebar._commands._seam import CommandError
    from rebar.llm import completion_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "drop fail", description="x" * 60, repo_root=r)

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(completion_sidecar, "emit", lambda *a, **k: False)

    fail_verdict = {
        "verdict": "FAIL",
        "ticket_id": tid,
        "findings": [{"criterion": "AC1", "detail": "unmet"}],
    }

    import rebar.llm as _llm

    monkeypatch.setattr(_llm, "verify_completion", lambda *a, **k: fail_verdict, raising=False)

    with pytest.raises(CommandError):
        close_precheck._completion_precheck(
            tid, "task", str(store), str(store), reason="", force_close=""
        )

    err = capsys.readouterr().err
    assert "Warning" in err and "WITHOUT" in err, (
        "a dropped FAIL sidecar must surface an explicit stderr warning"
    )
    assert tid.split("-")[0] in err or tid in err
