"""RP-05 S6 subprocess coverage for each intercept family after registry cutover.

Top-level help cases read committed artifacts without handler dispatch. Command cases prove
that registry execution metadata still reaches each handler. Every case checks its exit code
and command-specific output.
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
        check=False,
    )


# ── no-store families: a lightweight, store-free invocation per family ──
# (command args, expected exit, substring that must appear in stdout+stderr)
# Note: the legacy jira onboarding compatibility alias is intentionally NOT invoked here
# by its literal spelling (the repo's bridge-vocabulary gate forbids uncategorized legacy
# spellings in new files); its post-cutover dispatch is proven by the registry-derived
# parametrization in test_cli_registry_cutover_s6_heldout.py instead.
_NO_STORE_CASES = [
    ("review-plan", ("review-plan", "--help"), 0, "Usage: rebar review-plan"),
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
    assert "Traceback" not in combined, f"{label}: raised while serving the surface:\n{combined}"


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


def test_every_visible_intercept_has_two_store_free_committed_help_surfaces(
    tmp_path: Path,
) -> None:
    """Every visible intercept serves committed bytes through both top-level help forms."""
    from rebar._cli._registry import ROUTES

    repo_root = Path(__file__).resolve().parents[3]
    routes = [
        route
        for route in ROUTES
        if route.group == "intercept" and not route.hidden and not route.retired
    ]
    assert routes
    defects: list[str] = []
    for route in routes:
        artifact = repo_root / "src" / "rebar" / "_cli" / "help" / f"{route.name}.txt"
        expected = artifact.read_text(encoding="utf-8") if artifact.is_file() else None
        if expected is None:
            defects.append(f"{route.name} has no committed artifact")
        for label, args in (
            ("help-prefix", ("help", route.name)),
            ("command-help", (route.name, "--help")),
        ):
            root = tmp_path / route.name / label
            root.mkdir(parents=True)
            cp = _run(*args, cwd=root, env_overrides={"REBAR_ROOT": str(root)})
            if cp.returncode != 0:
                defects.append(f"{route.name} {label} exited {cp.returncode}")
            if expected is not None and cp.stdout != expected:
                defects.append(f"{route.name} {label} differed from committed bytes")
            if cp.stderr:
                defects.append(f"{route.name} {label} wrote stderr")
            if list(root.iterdir()):
                defects.append(f"{route.name} {label} created repository state")

    assert defects == []


@pytest.mark.parametrize("child", ["create", "use", "key"])
def test_identity_child_help_is_store_free(child: str, tmp_path: Path) -> None:
    """Identity child help exits successfully without creating repository state."""
    root = tmp_path / child
    root.mkdir()

    cp = _run(
        "identity",
        child,
        "--help",
        cwd=root,
        env_overrides={"REBAR_ROOT": str(root)},
    )

    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert cp.stdout.splitlines()[0].startswith(f"usage: rebar identity {child}")
    assert cp.stderr == ""
    assert list(root.iterdir()) == []
