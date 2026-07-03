"""The commit-message ticket gate (`rebar verify-commit-ticket`): extraction (incl. the
path-traversal security guard), resolution of every id form against a fixture store, the
config gate, merge-commit exemption, input modes + I/O errors, and doc-drift of the shared
`EXPECTED_FORMAT` constant.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar import config as cfg
from rebar._commands import verify_commit as vc

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "REBAR_CONFIG",
        "XDG_CONFIG_HOME",
        "REBAR_TRACKER_DIR",
        "REBAR_VERIFY_REQUIRE_TICKET_FOR_COMMIT",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg.reset_config_cache()


def _store(tmp_path: Path) -> Path:
    """A minimal ticket store: one ticket `abcd-1234-ef56-7890` aliased `my-alias`, bound to
    Jira key `REB-1`."""
    tracker = tmp_path / "store"
    tid = "abcd-1234-ef56-7890"
    (tracker / tid).mkdir(parents=True)
    (tracker / tid / "001-CREATE.json").write_text(
        json.dumps({"data": {"alias": "my-alias"}}), encoding="utf-8"
    )
    (tracker / ".bridge_state").mkdir()
    (tracker / ".bridge_state" / "bindings.json").write_text(
        json.dumps({"reverse": {"REB-1": tid}}), encoding="utf-8"
    )
    return tracker


# ── extraction ────────────────────────────────────────────────────────────────
def test_extract_trailer_with_parenthetical() -> None:
    refs = vc.extract_ticket_refs("feat: x\n\nrebar-ticket: fc9e-8c2e-cb2f-465f (blank-guild-koi)")
    assert "fc9e-8c2e-cb2f-465f" in refs and "blank-guild-koi" in refs


def test_extract_leading_subject_token() -> None:
    assert vc.extract_ticket_refs("blank-guild-koi: do the thing")[0] == "blank-guild-koi"


def test_extract_multiple_trailers() -> None:
    refs = vc.extract_ticket_refs("s\n\nrebar-ticket: a-alias\nrebar-ticket: REB-9")
    assert "a-alias" in refs and "REB-9" in refs


def test_extract_none() -> None:
    assert vc.extract_ticket_refs("just a plain sentence with no colon or trailer") == []


@pytest.mark.parametrize("evil", ["../../etc/passwd", "a/b", "..", "with space", "x\ty"])
def test_extract_drops_unsafe_candidates(evil: str) -> None:
    # A path-traversal / separator / whitespace candidate must never reach the resolver.
    refs = vc.extract_ticket_refs(f"subject\n\nrebar-ticket: {evil}")
    assert all(".." not in r and "/" not in r for r in refs)
    assert evil not in refs


# ── resolution against a fixture store (all id forms) ─────────────────────────
@pytest.mark.parametrize(
    "ref",
    [
        "abcd-1234-ef56-7890",  # full id
        "abcd-1234",  # short id (8-hex prefix)
        "my-alias",  # alias
        "abcd-1234-ef",  # unique prefix (>=4)
        "REB-1",  # Jira key via binding store
    ],
)
def test_resolves_every_id_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ref: str) -> None:
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(_store(tmp_path)))
    cfg.reset_config_cache()
    res = vc.verify_commit_message(f"subject\n\nrebar-ticket: {ref}")
    assert res.ok and res.resolved == "abcd-1234-ef56-7890"


def test_unresolvable_is_not_ok(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(_store(tmp_path)))
    cfg.reset_config_cache()
    assert not vc.verify_commit_message("chore: nothing\n\nrebar-ticket: nope-nope").ok


# ── CLI: config gate, diagnostics, merge exemption, input modes, I/O ──────────
def _enable(monkeypatch: pytest.MonkeyPatch, tracker: Path) -> None:
    monkeypatch.setenv("REBAR_VERIFY_REQUIRE_TICKET_FOR_COMMIT", "1")
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tracker))
    cfg.reset_config_cache()


def test_cli_gate_disabled_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the gate off (this repo's rebar.toml enables it) — disabling is the override/rollback.
    monkeypatch.setenv("REBAR_VERIFY_REQUIRE_TICKET_FOR_COMMIT", "0")
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(_store(tmp_path)))
    cfg.reset_config_cache()
    assert vc.cli(["--message", "no ticket at all"]) == 0


def test_cli_resolves_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, _store(tmp_path))
    assert vc.cli(["--message", "feat: x\n\nrebar-ticket: my-alias"]) == 0


def test_cli_missing_ticket_fails_with_documented_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _enable(monkeypatch, _store(tmp_path))
    # No trailer and no leading `<id>:` token → nothing extractable → "(none present)".
    rc = vc.cli(["--message", "forgot the ticket entirely"])
    err = capsys.readouterr().err
    assert rc == 1
    # The diagnostic DOCUMENTS the expected format, forms, an example, and the fix.
    assert vc.EXPECTED_FORMAT in err
    assert "rebar-ticket: <id>" in err and "blank-guild-koi" in err  # example
    assert "alias" in err and "Jira key" in err  # accepted forms
    assert "git commit --amend" in err  # the fix
    assert "(none present)" in err  # nothing was extractable


def test_cli_leading_id_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, _store(tmp_path))
    assert vc.cli(["--message", "my-alias: a subject-prefix commit"]) == 0


def test_cli_message_file_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, _store(tmp_path))
    mf = tmp_path / "msg.txt"
    mf.write_text("feat: y\n\nrebar-ticket: abcd-1234-ef56-7890", encoding="utf-8")
    assert vc.cli(["--message-file", str(mf)]) == 0


def test_cli_missing_message_file_is_infra_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _enable(monkeypatch, _store(tmp_path))
    rc = vc.cli(["--message-file", str(tmp_path / "nope.txt")])
    assert rc == 2  # distinguishable from the missing-ticket exit 1
    assert "cannot read" in capsys.readouterr().err


def test_cli_store_missing_is_infra_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("REBAR_VERIFY_REQUIRE_TICKET_FOR_COMMIT", "1")
    monkeypatch.setenv("REBAR_TRACKER_DIR", str(tmp_path / "absent-store"))
    cfg.reset_config_cache()
    rc = vc.cli(["--message", "feat: x\n\nrebar-ticket: my-alias"])
    assert rc == 2
    assert "store not found" in capsys.readouterr().err


# ── --rev input mode + merge-commit exemption (real git fixtures) ─────────────
def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    for a in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    return repo


def _commit(repo: Path, msg: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--allow-empty", "-q", "-m", msg],
        check=True,
        capture_output=True,
    )


def test_cli_rev_mode_resolves(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _enable(monkeypatch, _store(tmp_path))
    repo = _git_repo(tmp_path)
    _commit(repo, "feat: y\n\nrebar-ticket: my-alias")
    monkeypatch.chdir(repo)  # the CLI reads `git show` from cwd
    assert vc.cli(["--rev", "HEAD"]) == 0


def test_cli_bad_rev_is_infra_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _enable(monkeypatch, _store(tmp_path))
    monkeypatch.chdir(_git_repo(tmp_path))
    rc = vc.cli(["--rev", "does-not-exist"])
    assert rc == 2  # distinguishable from missing-ticket
    assert "git could not read" in capsys.readouterr().err


def test_cli_merge_commit_is_exempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _enable(monkeypatch, _store(tmp_path))
    repo = _git_repo(tmp_path)
    _commit(repo, "base")  # no ticket needed — the merge itself is exempt
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True, capture_output=True
    )
    _commit(repo, "feature work")
    subprocess.run(
        ["git", "-C", str(repo), "checkout", "-q", "main"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "merge", "--no-ff", "-q", "-m", "merge feature", "feature"],
        check=True,
        capture_output=True,
    )
    monkeypatch.chdir(repo)
    rc = vc.cli(["--rev", "HEAD"])  # HEAD is a 2-parent merge with no ticket
    assert rc == 0
    assert "merge commit" in capsys.readouterr().out


# ── doc-drift: the shared constant is quoted verbatim in the reference doc ─────
def test_expected_format_documented_in_reference() -> None:
    doc = Path(rebar.__file__).parents[2] / "docs" / "commit-ticket-trailer.md"
    assert vc.EXPECTED_FORMAT in doc.read_text(encoding="utf-8"), (
        "docs/commit-ticket-trailer.md must quote EXPECTED_FORMAT verbatim (single source of truth)"
    )
