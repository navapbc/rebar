"""Behavioral contracts for the comment-hygiene guard [rebar:b047-267f-3c3d-4374].

The detector blocks ROT-PRONE history in source comments/docstrings — commit SHAs
(Gerrit rebase-on-submit guarantees pre-land SHA death), run/job/thread ids (die
with retention windows), and dated incident narratives — while ACCEPTING blocks
that point history at the ticket system (grouped hex ids, word-triple aliases,
ADR ids) or carry the vendor-ref escape hatch. This module IS the guard's own
fixture corpus, so it deliberately contains live rot-prone tokens; the script
excludes exactly this file from the tree scan (EXCLUDED_FILES), which the
self-exclusion test below pins.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_comment_hygiene.py"


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# RED: each rot-prone trigger fires on an unaccepted block
# ---------------------------------------------------------------------------


def test_a_bare_commit_sha_in_a_comment_block_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# The fix landed as commit 62933ff62 and changed the deep-link shape.\nX = 1\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "src/mod.py" in result.stdout
    assert "62933ff62" in result.stdout


def test_a_bare_commit_sha_in_a_docstring_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        '"""Handles the cf93b2b7ad failure class from the binding store."""\nX = 1\n',
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "cf93b2b7ad" in result.stdout


def test_a_run_id_after_a_keyword_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# Reproduced in run 30721408463 before the oracle discriminated.\nX = 1\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "30721408463" in result.stdout


def test_a_dated_incident_narrative_fails(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# On 2025-01-15 the reconciler crashed when Jira dropped the parent.\nX = 1\n",
    )
    result = _run(tmp_path)
    assert result.returncode == 1


def test_the_failure_message_teaches_the_sanctioned_forms(tmp_path: Path) -> None:
    _write(tmp_path, "src/mod.py", "# Broken by commit deadbee1 last sprint.\nX = 1\n")
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "ticket" in result.stdout.lower()


# ---------------------------------------------------------------------------
# GREEN: acceptor arms — a triggered block citing the ticket system passes
# ---------------------------------------------------------------------------


def test_a_grouped_hex_ticket_id_accepts_the_block(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# The class of bug fixed for deep-links: see 3006-e198-77aa-4bb2.\n"
        "# Original failure narrated on the ticket, dated 2025-01-15, it failed hard.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_a_word_triple_alias_accepts_the_block(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# Census drift is recorded on robe-creek-zealot; run 30721408463 has details.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_an_adr_id_accepts_the_block(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# Rebase interaction reviewed under ADR 0047 after run 30763838558.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_the_external_context_escape_hatch_accepts_the_block(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# context: external — upstream fix is commit 8a1b2c3d4e in CPython's tree.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


# ---------------------------------------------------------------------------
# Gotchas measured in the planning prototype — must NOT trigger / NOT accept
# ---------------------------------------------------------------------------


def test_bug_surfaced_prose_does_not_trigger(tmp_path: Path) -> None:
    """'surfaced' is deliberately EXCLUDED from the evidence-verb set: it introduces
    current-state explanation, not an incident date."""
    _write(
        tmp_path,
        "src/mod.py",
        "# A bug surfaced on 2025-01-15 style dates is explained here for context.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_ticket_dirs_prose_is_not_an_acceptor(tmp_path: Path) -> None:
    """Prose containing the word 'ticket' (e.g. 'ticket dirs') without a resolvable id
    must not accept a triggered block."""
    _write(
        tmp_path,
        "src/mod.py",
        "# The ticket dirs moved after commit deadbee12 rewrote the layout.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 1


def test_a_hyphenated_file_path_is_not_an_alias_acceptor(tmp_path: Path) -> None:
    """A hyphen-joined FILE NAME must not read as a word-triple alias: the b047 close
    verification found a live raw SHA masked by 'docs/designs/sync-hardening-proposal.md'
    accepting its block."""
    _write(
        tmp_path,
        "src/mod.py",
        "# Canonical reference: 183fd51ac2; pending consolidation per\n"
        "# docs/designs/sync-hardening-proposal.md Item 3.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 1


def test_a_sentence_final_alias_still_accepts(tmp_path: Path) -> None:
    """The path/extension tightening must not reject a real alias that ends a
    sentence — '.' followed by whitespace is not a file extension."""
    _write(
        tmp_path,
        "src/mod.py",
        "# Run 30721408463's census drift is recorded on robe-creek-zealot. See there.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_hash_algorithm_names_do_not_trigger(tmp_path: Path) -> None:
    """ed25519 is 7 hex-alphabet chars with digits+letters — the denylist keeps
    algorithm names from reading as commit SHAs."""
    _write(
        tmp_path,
        "src/mod.py",
        "# Keys are ed25519; digests use sha256 over the canonical payload.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_a_bare_big_number_without_keyword_does_not_trigger(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# The backoff budget is 30000000 microseconds under contention.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_a_short_hex_group_does_not_trigger(tmp_path: Path) -> None:
    """4-char ticket-id fragments (0fad, e6a0) are far below the 7-char SHA floor."""
    _write(
        tmp_path,
        "src/mod.py",
        "# The 0fad pagination class; see also e6a0 for the prose-supersession shape.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_an_undated_evidence_verb_does_not_trigger(tmp_path: Path) -> None:
    """The date+verb trigger needs BOTH: a verb alone is current-state prose."""
    _write(
        tmp_path,
        "src/mod.py",
        "# This arm is taken when the transport failed and the walk degrades open.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_plain_current_state_prose_is_green(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "src/mod.py",
        "# Sorts by key so the diff is deterministic across passes.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


# ---------------------------------------------------------------------------
# Structural: self-exclusion + string literals out of scope
# ---------------------------------------------------------------------------


def test_the_guards_own_fixture_module_is_excluded(tmp_path: Path) -> None:
    """This module's fixtures MUST contain live rot-prone tokens; the script's
    EXCLUDED_FILES keeps exactly this path out of the tree scan."""
    _write(
        tmp_path,
        "tests/unit/test_comment_hygiene_guard.py",
        "# Fixture corpus: commit 62933ff62 failed on 2025-01-15 in run 30721408463.\nX = 1\n",
    )
    assert _run(tmp_path).returncode == 0


def test_string_literals_are_not_scanned(tmp_path: Path) -> None:
    """Only comments and docstrings are policy surface — runtime strings (e.g. test
    payloads) may carry hex freely."""
    _write(
        tmp_path,
        "src/mod.py",
        'PAYLOAD = {"sha": "62933ff62aabbccdd", "run": "run 30721408463"}\n',
    )
    assert _run(tmp_path).returncode == 0


@pytest.mark.repo_policy
def test_the_real_tree_is_clean() -> None:
    """AC: the guard passes the remediated tree with zero suppressions. Runs the
    script exactly as CI does."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"tree scan found rot-prone comments:\n{result.stdout}"


# ---------------------------------------------------------------------------
# Wiring: the gate runs in `make lint`, not only in CI / the full suite
# ---------------------------------------------------------------------------

_MAKEFILE = REPO_ROOT / "Makefile"
_GATE_INVOCATION = "scripts/check_comment_hygiene.py"


def _lint_target_body(makefile_text: str) -> str:
    """Recipe lines of the `lint` target only (the test_config_gate_wiring_heldout
    idiom): everything between `lint:` and the next target header."""
    import re

    body: list[str] = []
    in_target = False
    for line in makefile_text.splitlines():
        if re.match(r"^lint:", line):
            in_target = True
            continue
        if in_target and re.match(r"^[A-Za-z0-9_.-]+:", line):
            break
        if in_target:
            body.append(line)
    return "\n".join(body)


def test_make_lint_wires_the_hygiene_gate() -> None:
    """Ticket 2d9a-78c5-5f87-4a22: the local fast gate must surface comment-hygiene
    findings, not defer them to CI / the full suite."""
    body = _lint_target_body(_MAKEFILE.read_text(encoding="utf-8"))
    assert _GATE_INVOCATION in body, (
        "`make lint` does not invoke scripts/check_comment_hygiene.py — the local fast "
        "gate would return a clean verdict over a tree CI rejects (ticket 2d9a-78c5)."
    )


def test_wiring_check_detects_a_removed_invocation() -> None:
    """Teeth: stripping the invocation from a synthetic Makefile copy must flip the
    same assertion, and an invocation outside the lint target must not satisfy it."""
    mk = _MAKEFILE.read_text(encoding="utf-8")
    stripped = "\n".join(line for line in mk.splitlines() if _GATE_INVOCATION not in line)
    assert _GATE_INVOCATION not in _lint_target_body(stripped)
    relocated = stripped + f"\nother-target:\n\tpython {_GATE_INVOCATION}\n"
    assert _GATE_INVOCATION not in _lint_target_body(relocated)
