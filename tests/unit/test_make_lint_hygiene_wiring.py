"""Keep raw-git and wall-clock hygiene checks in local ``make lint``.

Both scripts also run in CI. The Makefile wiring prevents a local green gate
from missing either check."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MAKEFILE = _REPO_ROOT / "Makefile"

# Every script ``make lint`` runs: this module is their ONE wiring census (epic
# sainted-illjudged-fossa retired the per-script "make lint wires it" twins). Listed
# explicitly rather than discovered, so ADDING a CI-only script is not silently treated as a
# ``make lint`` obligation; ``test_every_make_lint_script_is_listed`` closes the other
# direction, so a newly wired script cannot go unlisted either.
_WIRED_GATES = (
    "scripts/check_adr_numbers.py",
    "scripts/check_cli_json_js_safe.py",
    "scripts/check_comment_hygiene.py",
    "scripts/check_complexity_baseline.py",
    "scripts/check_config_ownership.py",
    "scripts/check_config_reads.py",
    "scripts/check_criteria_vocabulary.py",
    "scripts/check_dco_identity.py",
    "scripts/check_deploy_manifest.py",
    "scripts/check_destructive_test_exec.py",
    "scripts/check_docs_index.py",
    "scripts/check_fcntl_import_guard.py",
    "scripts/check_mechanism_delta.py",
    "scripts/check_raw_git_writes.py",
    "scripts/check_repo_root_from_package.py",
    "scripts/check_repo_root_from_tracker.py",
    "scripts/check_shellcheck.py",
    "scripts/check_skill_frontmatter.py",
    "scripts/check_ssm_secret_state.py",
    "scripts/check_templatefile_escapes.py",
    "scripts/check_tickets_boundary.py",
    "scripts/check_user_data_size.py",
    "scripts/check_uv_pin.py",
    "scripts/check_verify_gate_parity.py",
    "scripts/check_wall_clock_asserts.py",
)


def _lint_target_body(makefile_text: str) -> str:
    """Recipe lines of the ``lint`` target only — everything between ``lint:`` and the next
    target header. Mirrors the ``test_comment_hygiene_guard`` idiom: an invocation parked
    under some *other* target must not count as wired."""
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


@pytest.mark.parametrize("script", _WIRED_GATES)
def test_make_lint_invokes_the_hygiene_gate(script: str) -> None:
    body = _lint_target_body(_MAKEFILE.read_text(encoding="utf-8"))
    assert script in body, (
        f"`make lint` does not invoke {script} — the local fast gate would return a clean "
        "verdict over a tree CI rejects (story 73b8-2d79-3046-44ce)."
    )


@pytest.mark.parametrize("script", _WIRED_GATES)
def test_the_wiring_check_has_teeth(script: str) -> None:
    """Stripping the invocation from a synthetic Makefile copy flips the same assertion, and
    an invocation parked outside the ``lint`` target does not satisfy it."""
    mk = _MAKEFILE.read_text(encoding="utf-8")
    stripped = "\n".join(line for line in mk.splitlines() if script not in line)
    assert script not in _lint_target_body(stripped)
    relocated = stripped + f"\nsome-other-target:\n\tpython {script}\n"
    assert script not in _lint_target_body(relocated)


_SCRIPT_CALL = re.compile(r"\bpython\s+(scripts/check_[A-Za-z0-9_]+\.py)")


def _lint_scripts(makefile_text: str) -> set[str]:
    return set(_SCRIPT_CALL.findall(_lint_target_body(makefile_text)))


def test_every_make_lint_script_is_listed() -> None:
    """The reverse direction: a ``scripts/check_*.py`` invoked by ``make lint`` but absent from
    ``_WIRED_GATES`` would have no wiring census at all once its pytest twin is gone. A
    synthetic script wired into a copy of the ``lint`` recipe must be reported as unlisted."""
    mk = _MAKEFILE.read_text(encoding="utf-8")
    assert _lint_scripts(mk) == set(_WIRED_GATES), (
        "`make lint` and _WIRED_GATES disagree: "
        f"unlisted={sorted(_lint_scripts(mk) - set(_WIRED_GATES))}, "
        f"not-run={sorted(set(_WIRED_GATES) - _lint_scripts(mk))}"
    )
    head, sep, tail = mk.partition("\nlint:")
    first, _, rest = tail.partition("\n")
    seeded = head + sep + first + "\n\tpython scripts/check_new.py\n" + rest
    assert _lint_scripts(seeded) - set(_WIRED_GATES) == {"scripts/check_new.py"}
