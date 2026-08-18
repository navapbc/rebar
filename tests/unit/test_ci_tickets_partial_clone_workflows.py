"""Workflow contracts for blobless tickets-branch CI fetches (B1 037b)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from _subprocess_env import subprocess_env

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[2]
_RECONCILE = _ROOT / ".github/workflows/reconcile-bridge.yml"
_VERIFY = _ROOT / ".github/workflows/verify-identity.yml"
_CANARY = _ROOT / ".github/workflows/reconcile-bridge-canary.yml"
_GERRIT = _ROOT / ".github/workflows/gerrit-verify.yaml"


def _workflow(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(path: Path, job: str) -> list[dict]:
    return _workflow(path)["jobs"][job]["steps"]


def _step(path: Path, job: str, name_fragment: str) -> dict:
    matches = [
        step
        for step in _steps(path, job)
        if name_fragment.lower() in str(step.get("name", "")).lower()
    ]
    assert len(matches) == 1, (
        f"expected one {path.name}:{job} step containing {name_fragment!r}, found {len(matches)}"
    )
    return matches[0]


@pytest.mark.parametrize(
    ("path", "job", "expected_inputs"),
    [
        (_RECONCILE, "reconcile", {"fetch-depth": 0, "persist-credentials": True}),
        (
            _CANARY,
            "canary",
            {"fetch-depth": 0, "persist-credentials": True, "ref": "main"},
        ),
        (_VERIFY, "verify-identity", {"fetch-depth": 0}),
    ],
)
def test_checkout_filter_and_preserves_inputs(
    path: Path, job: str, expected_inputs: dict[str, object]
) -> None:
    """Every all-history actions checkout is blobless without weakening prior inputs."""
    checkout = next(step for step in _steps(path, job) if step.get("uses") == "actions/checkout@v7")
    inputs = checkout.get("with") or {}
    assert inputs.get("filter") == "blob:none"
    for key, expected in expected_inputs.items():
        assert inputs.get(key) == expected, f"{path.name} changed checkout input {key}"


def _normalized_script(step: dict) -> str:
    return re.sub(r"\\\s*\n\s*", " ", str(step.get("run", "")))


def test_require_ticket_fetch_is_blobless_and_stays_depth_one() -> None:
    script = _normalized_script(_step(_GERRIT, "require-ticket", "resolvable rebar ticket"))
    command = next(line for line in script.splitlines() if "git fetch" in line)
    assert "--filter=blob:none" in command
    assert "--depth=1" in command
    assert '"https://github.com/${{ github.repository }}"' in command


def test_identity_fetch_is_blobless_and_full_history() -> None:
    script = _normalized_script(_step(_GERRIT, "verify-identity", "Mount the tickets store"))
    command = next(line for line in script.splitlines() if "git fetch" in line)
    assert "--filter=blob:none" in command
    assert not re.search(r"(?:^|\s)--depth(?:=|\s)", command)
    assert '"https://github.com/${{ github.repository }}"' in command


@pytest.mark.parametrize(
    ("path", "job", "limit_name", "limit"),
    [
        (_VERIFY, "verify-identity", "REBAR_CHECKOUT_PACK_LIMIT_KIB", 102400),
    ],
)
def test_pack_guard_contract(path: Path, job: str, limit_name: str, limit: int) -> None:
    """The production job declares a KiB limit and executes a fail-closed size-pack guard."""
    workflow = _workflow(path)
    job_def = workflow["jobs"][job]
    assert int((job_def.get("env") or {}).get(limit_name)) == limit
    guards = [
        step for step in job_def["steps"] if "git count-objects -v" in str(step.get("run", ""))
    ]
    assert guards, f"{path.name}:{job} has no executable pack-size guard"
    script = "\n".join(str(step["run"]) for step in guards)
    assert re.search(r"^size_pack=.*git count-objects -v", script, re.MULTILINE)
    assert limit_name in script
    assert re.search(r"\bexit 1\b", script)


def test_gerrit_verify_has_no_tickets_pack_gate() -> None:
    """The gerrit-verify tickets-pack gate was removed deliberately (a092).

    The `tickets` branch is an append-only event log mounted with FULL history
    (ADR 0051), so its pack only ever grows; a fixed `REBAR_TICKETS_PACK_LIMIT_KIB`
    ceiling is guaranteed to be crossed and, once crossed, casts Verified -1 on every
    Gerrit change repo-wide. This pins the deliberate absence so the fail-closed gate
    is not silently reintroduced without revisiting a092.
    """
    job_def = _workflow(_GERRIT)["jobs"]["verify-identity"]
    assert "REBAR_TICKETS_PACK_LIMIT_KIB" not in (job_def.get("env") or {})
    standalone_guards = [
        step
        for step in job_def["steps"]
        if "git count-objects -v" in str(step.get("run", ""))
        and "rebar verify-identity" not in str(step.get("run", ""))
    ]
    assert not standalone_guards, "the gerrit-verify tickets-pack gate must stay removed (a092)"


@pytest.mark.parametrize(
    ("path", "job", "verify_fragment"),
    [
        (_VERIFY, "verify-identity", "verify-identity (gating)"),
        (_GERRIT, "verify-identity", "verify-identity (config-driven posture"),
    ],
)
def test_identity_no_promisor_contract(path: Path, job: str, verify_fragment: str) -> None:
    """Identity scanning is bracketed by exact object-database snapshots."""
    steps = _steps(path, job)
    verify_index = next(
        i
        for i, step in enumerate(steps)
        if verify_fragment.lower() in str(step.get("name", "")).lower()
    )
    script = str(steps[verify_index].get("run", ""))
    assert script.count("git count-objects -v") == 2
    assert "rebar verify-identity" in script
    before = script.index("git count-objects -v")
    verify = script.index("rebar verify-identity")
    after = script.rindex("git count-objects -v")
    assert before < verify < after
    assert re.search(r"(?:cmp|diff)\s+[^\n]+", script)


_GUARD_CASES = [
    (_VERIFY, "verify-identity", "REBAR_CHECKOUT_PACK_LIMIT_KIB", "102400"),
]


def _standalone_guard(path: Path, job: str) -> str:
    matches = [
        str(step.get("run", ""))
        for step in _steps(path, job)
        if "git count-objects -v" in str(step.get("run", ""))
        and "rebar verify-identity" not in str(step.get("run", ""))
    ]
    assert len(matches) == 1, f"expected one standalone pack guard in {path.name}, got {matches}"
    return matches[0]


@pytest.mark.parametrize(("path", "job", "limit_name", "limit"), _GUARD_CASES)
@pytest.mark.parametrize(
    "fake_output",
    ["count: 0\nsize: 0\n", "size-pack: not-a-number\n"],
)
def test_pack_guard_fails_closed_on_malformed_count_objects(
    tmp_path: Path,
    path: Path,
    job: str,
    limit_name: str,
    limit: str,
    fake_output: str,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text(f"#!/bin/sh\nprintf '%s' '{fake_output}'\n", encoding="utf-8")
    git.chmod(0o755)
    env = subprocess_env({"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", limit_name: limit})
    result = subprocess.run(["bash", "-c", _standalone_guard(path, job)], cwd=tmp_path, env=env)
    assert result.returncode != 0


@pytest.mark.parametrize(("path", "job", "limit_name", "limit"), _GUARD_CASES)
def test_pack_guard_fails_closed_when_count_objects_errors(
    tmp_path: Path, path: Path, job: str, limit_name: str, limit: str
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    git = fake_bin / "git"
    git.write_text("#!/bin/sh\nexit 71\n", encoding="utf-8")
    git.chmod(0o755)
    env = subprocess_env({"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}", limit_name: limit})
    result = subprocess.run(["bash", "-c", _standalone_guard(path, job)], cwd=tmp_path, env=env)
    assert result.returncode != 0
