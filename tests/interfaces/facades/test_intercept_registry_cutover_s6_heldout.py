"""RP-05 S6 HELD-OUT E2E oracle — every intercept family still executes after the cutover.

Withheld from the implementation subagent; restored and run by the orchestrator. These are
the ADV1 teeth: a real subprocess ``rebar <intercept> …`` for EACH intercept command family
(reconcile / review-plan / enrich / config / verify-* / identity / audit / …), proving the
command still dispatches to its handler and produces its contractual exit code + output after
the explicit intercept ladder is gone. A route left with ``handler is None`` would instead
raise ``RuntimeError: … has no executable route`` (a traceback + failure), so a clean,
command-specific result is proof the cutover wired real execution metadata for that family.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env


def _run(
    *args: str, cwd: str | Path, env_overrides: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    env = subprocess_env({"REBAR_SYNC_PUSH": "off", **(env_overrides or {})})
    return subprocess.run(
        [sys.executable, "-m", "rebar.cli", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
        env=env,
    )


# ── no-store families: a lightweight, store-free invocation per family ──
# (command args, expected exit, substring that must appear in stdout+stderr)
# Note: the legacy jira onboarding compatibility alias is intentionally NOT invoked here
# by its literal spelling (the repo's bridge-vocabulary gate forbids uncategorized legacy
# spellings in new files); its post-cutover dispatch is proven by the registry-derived
# parametrization in test_cli_registry_cutover_s6_heldout.py instead.
_NO_STORE_CASES = [
    ("reconcile", ("reconcile", "--help"), 0, "usage: rebar_reconciler"),
    ("review-plan", ("review-plan", "--help"), 0, "usage: rebar review-plan"),
    ("review-code", ("review-code", "--help"), 0, "rebar review-code"),
    ("scan-spec", ("scan-spec", "--help"), 0, "rebar scan-spec"),
    ("verify-completion", ("verify-completion", "--help"), 0, "rebar verify-completion"),
    ("sign-review", ("sign-review", "--help"), 0, "rebar sign-review"),
    ("explain", ("explain", "plan"), 0, "plan"),
    ("verify-identity", ("verify-identity", "--help"), 0, "rebar verify-identity"),
    ("verify-authorship", ("verify-authorship", "--help"), 0, "rebar verify-authorship"),
    ("verify-opcert", ("verify-opcert", "--help"), 0, "rebar verify-opcert"),
    ("trusted-env", ("trusted-env", "--help"), 0, "rebar trusted-env"),
    ("remote-cert", ("remote-cert", "--help"), 0, "rebar remote-cert"),
    ("workflow", ("workflow", "--help"), 0, "rebar workflow"),
    ("llm", ("llm", "--help"), 0, "rebar llm"),
    ("prompt", ("prompt", "--help"), 0, "rebar prompt"),
    ("criteria", ("criteria", "--help"), 0, "rebar criteria"),
    ("config", ("config",), 0, "resolved configuration"),
    ("audit-help", ("audit", "--help"), 0, "rebar audit"),
]


@pytest.mark.parametrize(
    ("label", "args", "expected_rc", "needle"),
    _NO_STORE_CASES,
    ids=[c[0] for c in _NO_STORE_CASES],
)
def test_intercept_family_executes_no_store(
    label: str, args: tuple[str, ...], expected_rc: int, needle: str, tmp_path: Path
) -> None:
    cp = _run(*args, cwd=tmp_path, env_overrides={"REBAR_ROOT": str(tmp_path)})
    combined = cp.stdout + cp.stderr
    assert cp.returncode == expected_rc, f"{label}: rc={cp.returncode}\n{combined}"
    assert needle in combined, f"{label}: {needle!r} absent from output:\n{combined}"
    assert "has no executable route" not in combined, f"{label}: hit the None-handler RuntimeError"
    assert "Traceback" not in combined, f"{label}: raised instead of dispatching:\n{combined}"


# ── store-touching families: exercised in a real initialized repo ──


def test_enrich_family_executes(rebar_repo: Path) -> None:
    """``rebar enrich status`` dispatches through the registry and prints its JSON status."""
    cp = _run("enrich", "status", cwd=rebar_repo)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert '"pending"' in cp.stdout, cp.stdout + cp.stderr
    assert "has no executable route" not in (cp.stdout + cp.stderr)


def test_verify_commit_ticket_family_executes(rebar_repo: Path) -> None:
    """``rebar verify-commit-ticket`` (a store-touching pure intercept) executes."""
    cp = _run("verify-commit-ticket", "--rev", "HEAD", cwd=rebar_repo)
    combined = cp.stdout + cp.stderr
    assert cp.returncode in (0, 1), combined
    assert "verify-commit-ticket" in combined, combined
    assert "has no executable route" not in combined
    assert "Traceback" not in combined, combined


def test_identity_family_executes_and_inits(rebar_repo: Path) -> None:
    """``rebar identity create`` routes through the init-wrapping handler (validates + rejects
    a missing ``--name`` with the historical exit 1), proving the wrapper's full-init +
    dispatch path survived the ladder's removal."""
    cp = _run("identity", "create", "--email", "dev@example.com", cwd=rebar_repo)
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 1, combined
    assert "--name is required" in combined, combined
    assert "has no executable route" not in combined
    assert "Traceback" not in combined, combined


def test_audit_show_family_executes(rebar_repo: Path) -> None:
    """``rebar audit show`` dispatches to the audit handler (argparse rejects the missing
    positional with exit 2) — not the pinned-help pre-scan and not a None-handler raise."""
    cp = _run("audit", "show", cwd=rebar_repo)
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 2, combined
    assert "audit show" in combined, combined
    assert "has no executable route" not in combined
    assert "Traceback" not in combined, combined


def test_config_validate_family_executes(rebar_repo: Path) -> None:
    """``rebar config validate`` (excluded from the central store-mount) still dispatches."""
    cp = _run("config", "validate", cwd=rebar_repo)
    combined = cp.stdout + cp.stderr
    assert cp.returncode == 0, combined
    assert "config validate" in combined, combined
    assert "has no executable route" not in combined
