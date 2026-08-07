"""4802: an LLM sidecar's write-time secret-screen REFUSAL must surface LOUDLY, not be
swallowed indistinguishably from a generic emit failure.

THE DEFECT. The three LLM sidecars (completion / plan-review / code-review) wrap their
``append_event`` in ``except Exception: logger.warning(...); return False``. The write-time
secret screen (bug e7a9) raises from inside ``append_event`` when reviewed MATERIAL (a diff, a
plan, a finding detail) embeds a credential shape. That refusal was caught by the SAME broad
handler as a transient store error — logged once and dropped, with no distinct signal that the
gate's own audit trail was blocked by a policy refusal.

THE FIX (asserted here):
- the refusal is a distinct exception subclass (``SecretScreenRefused``, a ``CommandError``
  subclass so every ``except CommandError`` still catches it);
- each sidecar catches it FIRST and writes an EXPLICIT ``Warning: ... WITHOUT ...`` stderr line
  that names the ticket, states the audit record was BLOCKED by the write-time secret screen,
  and makes clear the record was NOT written;
- best-effort is preserved: ``emit`` still returns ``False`` and never raises, and a refusal is
  NOT retried like a transient LockTimeout.

Assertions are on OBSERVABLE behaviour (captured stderr, the returned bool), never a private
name.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import rebar

pytestmark = pytest.mark.unit

# A live AWS-access-key-id SHAPE that the write-time secret screen REFUSES. NOT the whitelisted
# published example (``AKIAIOSFODNN7EXAMPLE``); confirmed refused via ``screen_event_data``.
# Assembled from fragments so no contiguous key literal is committed (the runtime value is
# unchanged, so the screen still refuses it — see the guard test below); the inline allow is a
# belt-and-suspenders exemption for the deterministic code-review secret detector (this is a
# crafted NON-secret test fixture, not a real credential).
SECRET_TOKEN = "AKIA" + "1234567890" + "ABCDEF"  # gitleaks:allow — crafted test fixture


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


def test_secret_token_is_actually_refused_by_the_screen():
    """Guard: the crafted token must trigger the screen, else the RED tests below are vacuous."""
    from rebar.secret_screen import screen_event_data

    assert screen_event_data({"detail": f"leaked {SECRET_TOKEN} here"}), (
        "crafted token no longer triggers the secret screen; pick another"
    )


def test_completion_sidecar_secret_refusal_surfaces_loudly(store, monkeypatch, capsys):
    """A completion FAIL whose finding detail embeds a secret token is refused by the screen on
    write. ``emit`` must surface a LOUD, secret-screen-specific stderr warning naming the ticket,
    return ``False``, and NOT raise.

    RED on the pre-fix code: the refusal is caught by the generic ``except Exception`` handler,
    which only calls ``logger.warning`` (no stderr), so no explicit warning appears.
    """
    from rebar.llm import completion_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "secret work", description="x" * 60, repo_root=r)

    fail_verdict = {
        "verdict": "FAIL",
        "ticket_id": tid,
        "findings": [{"criterion": "AC1", "detail": f"reviewed diff leaked {SECRET_TOKEN}"}],
    }

    ok = completion_sidecar.emit(fail_verdict, repo_root=r)

    assert ok is False, "a screen refusal must return False (best-effort, never raise)"
    err = capsys.readouterr().err
    assert "Warning" in err and "WITHOUT" in err, (
        "a secret-screen refusal must surface an explicit stderr warning"
    )
    assert "secret screen" in err.lower(), (
        "the warning must name the write-time secret screen as the cause"
    )
    assert tid.split("-")[0] in err or tid in err, "the warning must name the ticket"


def test_completion_sidecar_secret_refusal_not_retried(store, monkeypatch, capsys):
    """A secret-screen refusal is DETERMINISTIC — it must not be retried like a transient
    LockTimeout. Count ``append_event`` calls: exactly one attempt before the loud surface."""
    from rebar._commands import _seam
    from rebar.llm import completion_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "no-retry work", description="x" * 60, repo_root=r)

    fail_verdict = {
        "verdict": "FAIL",
        "ticket_id": tid,
        "findings": [{"criterion": "AC1", "detail": f"secret {SECRET_TOKEN}"}],
    }

    real_append = _seam.append_event
    calls = {"n": 0}

    def counting_append(*a, **k):
        calls["n"] += 1
        return real_append(*a, **k)

    monkeypatch.setattr(_seam, "append_event", counting_append)

    ok = completion_sidecar.emit(fail_verdict, repo_root=r)

    assert ok is False
    assert calls["n"] == 1, "a deterministic screen refusal must NOT be retried"


def test_plan_review_sidecar_secret_refusal_surfaces_loudly(store, monkeypatch, capsys):
    from rebar.llm.plan_review import sidecar as plan_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "plan secret", description="x" * 60, repo_root=r)

    verdict = {
        "verdict": "REVISE",
        "ticket_id": tid,
        "coaching": [
            {"move_id": "m1", "subject": "secret", "coaching": f"plan text embeds {SECRET_TOKEN}"}
        ],
    }

    ok = plan_sidecar.emit(verdict, repo_root=r)

    assert ok is False
    err = capsys.readouterr().err
    assert "Warning" in err and "WITHOUT" in err
    assert "secret screen" in err.lower()
    assert tid.split("-")[0] in err or tid in err


def test_code_review_sidecar_secret_refusal_surfaces_loudly(store, monkeypatch, capsys):
    from rebar.llm.code_review import sidecar as code_sidecar

    r = str(store)
    tid = rebar.create_ticket("task", "code secret", description="x" * 60, repo_root=r)

    verdict = {
        "verdict": "BLOCK",
        "ticket_id": tid,
        "coaching": [f"diff hunk leaked {SECRET_TOKEN}"],
    }

    ok = code_sidecar.emit(verdict, target_ticket=tid, repo_root=r)

    assert ok is False
    err = capsys.readouterr().err
    assert "Warning" in err and "WITHOUT" in err
    assert "secret screen" in err.lower()
    assert tid.split("-")[0] in err or tid in err
