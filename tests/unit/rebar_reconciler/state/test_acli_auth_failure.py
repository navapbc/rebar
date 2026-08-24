"""Bug sole-curbable-stinkpot — ACLI credential rejection must fast-abort and be
reported as a CREDENTIAL problem, not a generic data failure.

Six scheduled Reconcile Bridge runs over 2026-08-07 -> 2026-08-11 died on the
FIRST ACLI call of the pass with::

    ✗ Error: unauthorized: use `acli [product] auth login` to authenticate

The historical fast-abort was keyed on ``returncode == 401``. ACLI is a
subprocess and exits **1**, so that branch was dead code: the deterministic
failure burned the full 3-attempt retry budget and then surfaced as a generic
``returned non-zero exit status 1``.

The fakes follow the sibling timeout suite's shape: tiny ``python -c`` programs
invoked as the ``acli`` binary via ``acli_cmd=[sys.executable, "-c", ...]``.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from rebar_reconciler import __main__ as reconciler_main
from rebar_reconciler.adapters.jira import acli as acli_mod
from rebar_reconciler.adapters.jira import acli_subprocess

# The exact stderr ACLI emits on a rejected credential, as captured in the failed
# bridge runs (the ✗ prefix and backticks included, verbatim).
LIVE_AUTH_STDERR = "✗ Error: unauthorized: use `acli [product] auth login` to authenticate"

# Records one marker byte per invocation (so we can count spawns), then exits
# non-zero with caller-supplied stderr — the shape of a real ACLI failure.
_COUNT_THEN_FAIL = r"""
import sys
with open(sys.argv[1], "a") as f:
    f.write("x")
sys.stderr.write(sys.argv[2])
raise SystemExit(1)
"""


def _fake_acli(countfile, stderr_text: str) -> list[str]:
    """An ``acli_cmd`` prefix that counts invocations then fails with *stderr_text*."""
    return [sys.executable, "-c", _COUNT_THEN_FAIL, str(countfile), stderr_text]


def _spawn_count(countfile) -> int:
    return len(countfile.read_text()) if countfile.exists() else 0


# ---------------------------------------------------------------------------
# The marker predicate
# ---------------------------------------------------------------------------


def test_live_unauthorized_stderr_is_detected_as_auth_failure():
    """The verbatim stderr from the failed bridge runs must be recognised."""
    assert acli_subprocess._is_auth_failure(LIVE_AUTH_STDERR) is True


@pytest.mark.parametrize(
    "stderr_text",
    [
        "Error: unauthorized",
        "UNAUTHORIZED: token rejected",  # case-insensitive
        "use `acli jira auth login` to authenticate",
        "authentication failed",
        "invalid credentials",
    ],
)
def test_credential_rejection_wording_is_detected(stderr_text: str):
    assert acli_subprocess._is_auth_failure(stderr_text) is True


@pytest.mark.parametrize(
    "stderr_text",
    [
        None,
        "",
        "Error: project does not exist",
        "Error: cannot be assigned",  # a permission/data fault, NOT a credential fault
        "Error: 429 too many requests",
        "Error: command cancelled",
    ],
)
def test_non_credential_stderr_is_not_an_auth_failure(stderr_text):
    """Kept tight on purpose: a data/permission fault must not be reported to an
    operator as an expired credential."""
    assert acli_subprocess._is_auth_failure(stderr_text) is False


# ---------------------------------------------------------------------------
# The transport-layer fast-abort
# ---------------------------------------------------------------------------


def test_auth_failure_raises_typed_error_not_called_process_error(tmp_path):
    """A rejected credential must surface as AcliAuthError so callers can tell it
    apart from any other non-zero exit."""
    countfile = tmp_path / "spawns"
    with pytest.raises(acli_subprocess.AcliAuthError) as excinfo:
        acli_subprocess._run_acli(
            ["jira", "workitem", "search"],
            acli_cmd=_fake_acli(countfile, LIVE_AUTH_STDERR),
        )
    err = excinfo.value
    assert "jira" in err.cmd and "workitem" in err.cmd
    assert "unauthorized" in (err.stderr or "")
    # The operator-facing text must name the condition, not just echo argv.
    assert "credential" in str(err).lower()


def test_auth_failure_aborts_on_the_first_attempt(tmp_path):
    """THE REGRESSION. Exit code is 1 (not 401), which the old
    ``returncode == _AUTH_FAILURE_CODE`` branch never matched — so this used to
    burn all 3 attempts with 2s+4s backoff before failing anyway."""
    countfile = tmp_path / "spawns"
    with pytest.raises(acli_subprocess.AcliAuthError):
        acli_subprocess._run_acli(
            ["jira", "workitem", "search"],
            acli_cmd=_fake_acli(countfile, LIVE_AUTH_STDERR),
        )
    assert _spawn_count(countfile) == 1, "a rejected credential is deterministic — do not retry"


def test_auth_failure_chains_the_underlying_process_error(tmp_path):
    """PEP 3134: the CalledProcessError stays reachable for diagnostics."""
    countfile = tmp_path / "spawns"
    with pytest.raises(acli_subprocess.AcliAuthError) as excinfo:
        acli_subprocess._run_acli(["jira", "x"], acli_cmd=_fake_acli(countfile, LIVE_AUTH_STDERR))
    assert isinstance(excinfo.value.__cause__, subprocess.CalledProcessError)


def test_auth_error_is_reexported_from_the_acli_surface():
    """Callers import the adapter surface, not the transport module."""
    assert acli_mod.AcliAuthError is acli_subprocess.AcliAuthError


def test_non_auth_failure_still_retries_the_full_budget(tmp_path, monkeypatch):
    """Regression guard: the fast-abort must not swallow ordinary transient
    failures — those keep the existing retry-and-backoff behaviour."""
    slept: list[float] = []
    monkeypatch.setattr(acli_subprocess, "_backoff_sleep", lambda s: slept.append(s))
    countfile = tmp_path / "spawns"
    with pytest.raises(subprocess.CalledProcessError):
        acli_subprocess._run_acli(
            ["jira", "workitem", "search"],
            acli_cmd=_fake_acli(countfile, "Error: transient upstream glitch"),
        )
    assert _spawn_count(countfile) == acli_subprocess._MAX_ATTEMPTS == 3
    assert slept == [2, 4]


# ---------------------------------------------------------------------------
# The pass-level classification an operator actually reads
# ---------------------------------------------------------------------------


def test_auth_failure_classifies_as_config_not_operational():
    """AC: an operator can tell a credential problem from a data problem without
    opening the log — distinct disposition (exit 2, not 1) AND distinct message."""
    exc = acli_subprocess.AcliAuthError(["acli", "jira", "workitem", "search"], LIVE_AUTH_STDERR)
    result = reconciler_main._reconcile_exception_result(
        exc, reschedule_error_cls=None, lock_lost_cls=None
    )
    assert result.disposition is reconciler_main._Disposition.INVALID_INVOCATION
    assert result.disposition.canonical_exit == 2
    assert result.details["error_class"] == "auth_failed"
    message = result.legacy_message or ""
    assert "JIRA_API_TOKEN" in message, "the message must name the secret to rotate"
    assert "credential problem, not a data problem" in message
    # It must NOT be the generic phrasing the six failed runs reported.
    assert "reconcile_once raised" not in message


def test_ordinary_failure_keeps_the_generic_operational_classification():
    """Regression guard: only auth failures get the new treatment."""
    result = reconciler_main._reconcile_exception_result(
        RuntimeError("some data problem"), reschedule_error_cls=None, lock_lost_cls=None
    )
    assert result.disposition is reconciler_main._Disposition.OPERATIONAL_FAILURE
    assert "error_class" not in result.details
    assert "reconcile_once raised" in (result.legacy_message or "")


# ---------------------------------------------------------------------------
# REB-3115 S1 T2 (AC5) — the real exit-1 unauthorized shape decodes deterministically
# ---------------------------------------------------------------------------
#
# ``decode_acli_triple`` is the deterministic classifier over historical
# ``(exit, stdout, stderr)`` triples. The real bridge-run credential rejection —
# exit 1 with the ✗ unauthorized stderr captured verbatim above — must decode to
# the ``auth_failure`` class (never a generic retryable failure, never
# fail-loud).


def test_decode_real_exit1_unauthorized_is_auth_failure():
    outcome = acli_subprocess.decode_acli_triple(1, "", LIVE_AUTH_STDERR)
    assert outcome is acli_subprocess.AcliOutcome.auth_failure


@pytest.mark.parametrize(
    "stderr_text",
    [
        "✗ Error: unauthorized: use `acli [product] auth login` to authenticate",
        "authentication failed",
        "Error: invalid credentials",
        "please run `acli jira auth login`",
    ],
)
def test_decode_credential_rejection_wording_is_auth_failure(stderr_text: str):
    assert (
        acli_subprocess.decode_acli_triple(1, "", stderr_text)
        is acli_subprocess.AcliOutcome.auth_failure
    )


def test_decode_unknown_nonzero_shape_fails_loud_and_does_not_retry():
    with pytest.raises(acli_subprocess.UnknownAcliOutcomeError):
        acli_subprocess.decode_acli_triple(1, "", "Error: something entirely unexpected happened")
    with pytest.raises(acli_subprocess.UnknownAcliOutcomeError):
        acli_subprocess.decode_acli_triple(3, "", "")
