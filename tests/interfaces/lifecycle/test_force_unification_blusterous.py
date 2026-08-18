"""Coverage for the unified ``--force[=<reason>]`` surface (ticket blusterous-earthly-kitten).

``transition`` was brought to ``claim``'s canonical shape: one force-bypass surface whose
value IS the audit reason, spelled identically across the two verbs and across the library
and CLI. The redundant ``force_close`` parameter and the ``force`` boolean were retired to
deprecation aliases, and ``--reason`` no longer doubles as the force-bypass note (it feeds
only ``close_reason``). These tests pin:

  * a bare ``--force`` records ``"(no reason given)"`` — byte-identically for claim and
    transition, over BOTH the library and the CLI;
  * ``--force=<reason>`` records ``<reason>`` — again identically for both verbs;
  * ``--reason`` on a force close is NOT smuggled in as the force note;
  * the legacy ``rebar.transition(force_close=...)`` / ``force=True`` calls WARN and map onto
    the unified ``force`` string.
"""

from __future__ import annotations

import subprocess
import warnings
from pathlib import Path

import rebar
from rebar import _cli
from rebar._cli import _help

_DESC = (
    "A sufficiently detailed plan body.\n\n## Approach\nDo the thing carefully.\n\n"
    "## Scope\nsrc/x.py\n\n## Testing\n`pytest -q`\n\n## Acceptance Criteria\n"
    "- [ ] the thing works (checked: `pytest -q`)\n"
)


def _enable_claim_gate(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_plan_review_for_claim = true\n")


def _enable_close_gate(repo: Path) -> None:
    (repo / "rebar.toml").write_text("[verify]\nrequire_completion_verification_for_close = true\n")


def _commit(repo: Path) -> None:
    subprocess.run(
        ["git", "commit", "--allow-empty", "-q", "-m", "c"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )


def _make(repo: Path, ttype: str = "task") -> str:
    return rebar.create_ticket(ttype, f"plan {ttype}", description=_DESC, repo_root=str(repo))


def _status(tid: str, repo: Path) -> str:
    return rebar.show_ticket(tid, repo_root=str(repo))["status"]


def _audit(tid: str, repo: Path) -> str:
    return " ".join(
        c.get("body", "") for c in rebar.show_ticket(tid, repo_root=str(repo)).get("comments", [])
    )


# ── bare --force records the SAME placeholder for claim and transition ───────────
def test_bare_force_note_identical_claim_and_transition_library(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable_claim_gate(rebar_repo)
    tid_claim = _make(rebar_repo)
    tid_txn = _make(rebar_repo)

    rebar.claim(tid_claim, force="(no reason given)", repo_root=str(rebar_repo))
    rebar.transition(
        tid_txn, "open", "in_progress", force="(no reason given)", repo_root=str(rebar_repo)
    )

    assert _status(tid_claim, rebar_repo) == "in_progress"
    assert _status(tid_txn, rebar_repo) == "in_progress"
    assert '"(no reason given)"' in _audit(tid_claim, rebar_repo)
    assert '"(no reason given)"' in _audit(tid_txn, rebar_repo)


def test_bare_force_note_identical_claim_and_transition_cli(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable_claim_gate(rebar_repo)
    tid_claim = _make(rebar_repo)
    tid_txn = _make(rebar_repo)

    assert _cli.main(["claim", tid_claim, "--force"]) == 0
    assert _cli.main(["transition", tid_txn, "open", "in_progress", "--force"]) == 0

    claim_note = _audit(tid_claim, rebar_repo)
    txn_note = _audit(tid_txn, rebar_repo)
    assert '"(no reason given)"' in claim_note
    assert '"(no reason given)"' in txn_note


def test_force_reason_note_identical_claim_and_transition_cli(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable_claim_gate(rebar_repo)
    tid_claim = _make(rebar_repo)
    tid_txn = _make(rebar_repo)

    assert _cli.main(["claim", tid_claim, "--force=deadline"]) == 0
    assert _cli.main(["transition", tid_txn, "open", "in_progress", "--force=deadline"]) == 0

    assert '"deadline"' in _audit(tid_claim, rebar_repo)
    assert '"deadline"' in _audit(tid_txn, rebar_repo)


# ── --reason no longer feeds the force-bypass note ───────────────────────────────
def test_reason_not_used_as_force_note_on_close(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    tid = _make(rebar_repo)
    rebar.claim(tid, repo_root=str(rebar_repo))
    _enable_close_gate(rebar_repo)

    # A force close carrying --reason: the reason must NOT become the FORCE_CLOSE note.
    argv = ["transition", tid, "in_progress", "closed", "--force", "--reason=some rationale"]
    assert _cli.main(argv) == 0
    assert _status(tid, rebar_repo) == "closed"
    audit = _audit(tid, rebar_repo)
    assert "FORCE_CLOSE" in audit
    assert '"(no reason given)"' in audit
    assert "some rationale" not in audit
    # closed WITHOUT a completion signature — the durable "validation did not pass" signal.
    assert rebar.verify_signature(tid, repo_root=str(rebar_repo))["verdict"] == "unsigned"


def test_transition_help_keeps_reason_separate_from_force() -> None:
    help_text = _help.subcommand_help("transition") or ""
    reason_line = next(
        line for line in help_text.splitlines() if line.lstrip().startswith("--reason")
    )

    assert "close" in reason_line
    assert "--class" in reason_line
    assert "with --force" not in reason_line
    assert "gate-bypass audit note" not in reason_line


def test_force_close_reason_from_force_value(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    tid = _make(rebar_repo)
    rebar.claim(tid, repo_root=str(rebar_repo))
    _enable_close_gate(rebar_repo)

    assert _cli.main(["transition", tid, "in_progress", "closed", "--force=verifier flaky"]) == 0
    assert _status(tid, rebar_repo) == "closed"
    audit = _audit(tid, rebar_repo)
    assert "FORCE_CLOSE" in audit and '"verifier flaky"' in audit


# ── legacy library spellings WARN and map onto the unified force ─────────────────
def test_legacy_force_close_kwarg_warns_and_maps(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    tid = _make(rebar_repo)
    rebar.claim(tid, repo_root=str(rebar_repo))
    _enable_close_gate(rebar_repo)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        out = rebar.transition(
            tid, "in_progress", "closed", force_close="lib override", repo_root=str(rebar_repo)
        )
    assert out["to"] == "closed"
    assert any(
        "force_close" in str(w.message) and issubclass(w.category, DeprecationWarning)
        for w in caught
    )
    audit = _audit(tid, rebar_repo)
    assert "FORCE_CLOSE" in audit and '"lib override"' in audit


def test_legacy_force_bool_warns_and_maps(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    _enable_claim_gate(rebar_repo)
    tid = _make(rebar_repo)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        rebar.transition(tid, "open", "in_progress", force=True, repo_root=str(rebar_repo))
    assert _status(tid, rebar_repo) == "in_progress"
    assert any(
        "force=True" in str(w.message) and issubclass(w.category, DeprecationWarning)
        for w in caught
    )
    # bare boolean force maps to the placeholder note, identical to a bare --force.
    assert '"(no reason given)"' in _audit(tid, rebar_repo)


# ── force="" is a present force with the same placeholder on both verbs ──────────
def test_empty_string_force_bypasses_claim_and_transition_identically(
    rebar_repo: Path,
) -> None:
    _commit(rebar_repo)
    _enable_claim_gate(rebar_repo)
    tid_claim = _make(rebar_repo)
    tid_txn = _make(rebar_repo)

    # ``None`` alone means absent in the public str|None contract. A supplied empty
    # string is a present force with no usable reason, so both verbs record the same
    # audit-safe placeholder used by a bare CLI flag.
    rebar.claim(tid_claim, force="", repo_root=str(rebar_repo))
    rebar.transition(tid_txn, "open", "in_progress", force="", repo_root=str(rebar_repo))
    assert _status(tid_claim, rebar_repo) == "in_progress"
    assert _status(tid_txn, rebar_repo) == "in_progress"
    assert '"(no reason given)"' in _audit(tid_claim, rebar_repo)
    assert '"(no reason given)"' in _audit(tid_txn, rebar_repo)


# ── explicit force wins over the deprecated force_close alias when both are given ─
def test_force_wins_over_force_close_when_both_supplied(rebar_repo: Path) -> None:
    _commit(rebar_repo)
    tid = _make(rebar_repo)
    rebar.claim(tid, repo_root=str(rebar_repo))
    _enable_close_gate(rebar_repo)

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        rebar.transition(
            tid,
            "in_progress",
            "closed",
            force="new wins",
            force_close="legacy loses",
            repo_root=str(rebar_repo),
        )
    assert _status(tid, rebar_repo) == "closed"
    # the deprecated alias still warns, but the explicit unified force is what is recorded.
    assert any(
        "force_close" in str(w.message) and issubclass(w.category, DeprecationWarning)
        for w in caught
    )
    audit = _audit(tid, rebar_repo)
    assert '"new wins"' in audit
    assert "legacy loses" not in audit
