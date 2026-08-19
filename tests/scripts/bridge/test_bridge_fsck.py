"""Happy-path oracle for the offline bridge fsck contract (ticket 030f)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.scripts]

_ENV_ID = "bbbbbbbb-0000-4000-8000-000000000002"


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _write_event(tracker: Path, local_id: str, event_type: str = "CREATE") -> None:
    ticket_dir = tracker / local_id
    ticket_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "event_type": event_type,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "timestamp": 1_800_000_000_000_000_000,
        "author": "test-author",
        "env_id": _ENV_ID,
        "data": {
            "ticket_type": "task",
            "title": "Known ticket",
            "parent_id": None,
        },
    }
    (ticket_dir / f"1-{payload['uuid']}-{event_type}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _init_committed_tracker(tmp_path: Path) -> Path:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _git(tracker, "init", "-q", "-b", "tickets")
    _git(tracker, "config", "user.email", "test@example.com")
    _git(tracker, "config", "user.name", "Test")
    return tracker


def _commit(tracker: Path) -> None:
    _git(tracker, "add", ".")
    _git(tracker, "commit", "-q", "-m", "fixture")


def _write_consistent_binding(tracker: Path, local_id: str, jira_key: str) -> None:
    bridge_state = tracker / ".bridge_state"
    bridge_state.mkdir()
    (bridge_state / "bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    local_id: {
                        "state": "confirmed",
                        "jira_key": jira_key,
                        "baseline": {},
                    }
                },
                "reverse": {jira_key: local_id},
            }
        ),
        encoding="utf-8",
    )


def test_clean_committed_store_returns_only_the_new_contract(tmp_path: Path) -> None:
    """Known committed events plus consistent indexes are exactly clean."""
    from rebar._engine_support import bridge_fsck

    tracker = _init_committed_tracker(tmp_path)
    _write_event(tracker, "loc-clean")
    _write_consistent_binding(tracker, "loc-clean", "REB-1")
    _commit(tracker)

    findings = bridge_fsck.audit_bridge_mappings(tracker)

    assert set(findings) == {"unknown_event_types", "binding_drift", "store_integrity"}
    assert findings["unknown_event_types"] == []
    assert findings["store_integrity"] == []


def test_clean_cli_json_uses_the_new_contract_and_exits_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The human/JSON command path consumes the same clean offline result."""
    from rebar._engine_support import bridge_fsck

    tracker = _init_committed_tracker(tmp_path)
    _write_event(tracker, "loc-cli")
    _write_consistent_binding(tracker, "loc-cli", "REB-2")
    _commit(tracker)

    rc = bridge_fsck.main(["--tickets-tracker", str(tracker), "--output", "json"])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert set(out) == {"unknown_event_types", "binding_drift", "store_integrity"}
    assert out["store_integrity"] == []


# ── Opt-in live mapped-project visibility step (ticket 9702) ──────────────────


class _FakeProbe:
    """A fake Jira Cloud REST accessor exposing the ``_direct_rest_get`` seam.

    Returns a single-page PageBean of the configured visible project keys, so the
    shared ``check_mapped_project_visibility`` helper can be driven without any
    network access.
    """

    def __init__(self, visible_keys: list[str]) -> None:
        self._visible = visible_keys
        self.calls: list[str] = []

    def _direct_rest_get(self, path: str) -> dict:
        self.calls.append(path)
        return {
            "values": [{"key": key} for key in self._visible],
            "isLast": True,
            "total": len(self._visible),
        }


def _write_projects_mapping(
    tmp_path: Path, projects: dict[str, list[str]], legacy_default: str | None
) -> Path:
    """Seed ``.tickets-tracker/.bridge_state/projects.json`` under a repo root."""
    tracker = tmp_path / ".tickets-tracker"
    bridge_state = tracker / ".bridge_state"
    bridge_state.mkdir(parents=True, exist_ok=True)
    (bridge_state / "projects.json").write_text(
        json.dumps(
            {
                "projects": {key: {"repos": repos} for key, repos in projects.items()},
                "legacy_default": legacy_default,
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_live_visibility_reports_invisible_mapped_key(tmp_path: Path) -> None:
    """(a) Injected probe missing a mapped key → verdict status='missing' names it."""
    from rebar._engine_support import bridge_fsck_visibility

    repo_root = _write_projects_mapping(
        tmp_path, {"VIS": ["repo-a"], "GONE": ["repo-b"]}, legacy_default="VIS"
    )
    probe = _FakeProbe(["VIS"])  # GONE not listed → invisible to the bot

    verdict = bridge_fsck_visibility.audit_mapped_project_visibility(repo_root, probe=probe, env={})

    assert verdict["status"] == "missing"
    assert "GONE" in verdict["missing"]
    assert "VIS" not in verdict["missing"]
    assert probe.calls, "the shared helper must have queried the injected probe"


def test_live_visibility_all_visible_is_ok(tmp_path: Path) -> None:
    """(c) Injected probe listing every required key → verdict status='ok'."""
    from rebar._engine_support import bridge_fsck_visibility

    repo_root = _write_projects_mapping(tmp_path, {"VIS": ["repo-a"]}, legacy_default="DIG")
    probe = _FakeProbe(["VIS", "DIG", "OTHER"])

    verdict = bridge_fsck_visibility.audit_mapped_project_visibility(repo_root, probe=probe, env={})

    assert verdict["status"] == "ok"
    assert verdict["missing"] == []


def test_live_visibility_skips_when_credentials_absent(tmp_path: Path) -> None:
    """(b) No probe + absent JIRA_* creds → advisory skip, no probe call, no crash."""
    from rebar._engine_support import bridge_fsck_visibility

    repo_root = _write_projects_mapping(tmp_path, {"VIS": ["repo-a"]}, legacy_default="DIG")

    def _explode(*_args: object, **_kwargs: object) -> None:  # pragma: no cover - must not run
        raise AssertionError("the probe must not be built when credentials are absent")

    fake_access_check = type(
        "_FakeAccessCheck",
        (),
        {"check_mapped_project_visibility": staticmethod(_explode)},
    )
    verdict = bridge_fsck_visibility.audit_mapped_project_visibility(
        repo_root,
        probe=None,
        env={"JIRA_URL": "", "JIRA_USER": "", "JIRA_API_TOKEN": ""},
        access_check_mod=fake_access_check,
    )

    assert verdict["status"] == "skipped"
    assert verdict["reason"] == "missing_credentials"


def test_live_visibility_cli_flag_stderr_advisory_keeps_json_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--live-visibility renders the advisory to stderr; stdout JSON stays the 3-key contract."""
    from rebar._engine_support import bridge_fsck

    tracker = _init_committed_tracker(tmp_path)
    _write_event(tracker, "loc-vis")
    _write_consistent_binding(tracker, "loc-vis", "REB-9")
    _commit(tracker)

    rc = bridge_fsck.main(
        ["--tickets-tracker", str(tracker), "--output", "json", "--live-visibility"]
    )
    captured = capsys.readouterr()
    out = json.loads(captured.out)

    assert rc == 0
    assert set(out) == {"unknown_event_types", "binding_drift", "store_integrity"}
    # No live creds in the test env → advisory skip line goes to stderr.
    assert "live" in captured.err.lower() or "visib" in captured.err.lower()


def test_core_doctor_stays_jira_free() -> None:
    """(d) Core `rebar doctor` must not reach any reconciler / Jira import."""
    import ast
    from pathlib import Path as _P

    import rebar._commands.doctor as doctor_mod

    source = _P(doctor_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = ("rebar_reconciler", "access_check", "acli")
    offenders = [name for name in imported if any(tok in name for tok in forbidden)]
    assert not offenders, f"core doctor must stay Jira-free; found: {offenders}"
