"""Transition uses one reason-bearing --force escape hatch (24f7).

It replaces the separate start-work --force and close --force-close flags,
matching claim. The hand-written argument loop can silently skip unknown
tokens, so it must explicitly reject retired --force-close rather than
ignore an operator's bypass request."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._commands._seam import CommandError
from rebar._commands.transition import _parse_flags

pytestmark = pytest.mark.allow_unharnessed_subprocess(
    "the shared `_tracked_files` helper enumerates this checkout's git-tracked files\n"
    "so anything newly committed is in scope"
)

# Assembled from parts so a tree-wide grep for the dead flag spelling stays clean.
RETIRED = "--force" + "-close"


def _force_reason(argv: list[str]) -> str | None:
    """The `force_reason` slot of `_parse_flags` — `None` iff `--force` was absent."""
    return _parse_flags(argv)[1]


# ── The new spelling ─────────────────────────────────────────────────────────────


def test_force_with_a_reason_carries_that_reason() -> None:
    """`--force="<reason>"` is the direct replacement for the retired close-only flag."""
    assert _force_reason(["--force=verifier timed out"]) == "verifier timed out"


def test_bare_force_is_present_but_empty() -> None:
    """A bare `--force` must be distinguishable from an ABSENT `--force`.

    The close path tests `force_close` for truthiness, so the CLI wrapper defaults a bare
    `--force` to the `(no reason given)` placeholder `claim --force` uses. That is only
    possible if the parser reports "present, no value" (`""`) rather than "absent"
    (`None`) — this is the distinction the whole dispatch rests on.
    """
    assert _force_reason(["--force"]) == ""


def test_absent_force_is_none() -> None:
    assert _force_reason(["--reason=nothing forced here"]) is None


def test_force_does_not_swallow_a_following_token() -> None:
    """`--force` takes its reason ONLY via `=`, never as the next argv token.

    `claim --force` behaves the same way. If `--force` consumed the next token, a bare
    `--force --class=regression` would silently eat the class and the bug close would then
    fail a required-`--class` check for a reason the operator cannot see.
    """
    reason, force_reason, close_class, _caused_by, _ref = _parse_flags(
        ["--force", "--class=regression"]
    )
    assert force_reason == ""
    assert close_class == "regression"
    assert reason == ""


def test_force_reason_and_reason_are_independent_slots() -> None:
    """`--force=X --reason=Y` keeps both: Y is the transition reason, X the force note."""
    reason, force_reason, _class, _caused_by, _ref = _parse_flags(
        ["--force=gate offline", "--reason=closing per operator"]
    )
    assert force_reason == "gate offline"
    assert reason == "closing per operator"


def test_force_reason_may_be_explicitly_empty() -> None:
    """`--force=` is "present with an empty value" — still present, not absent."""
    assert _force_reason(["--force="]) == ""


# ── The retired spelling is REJECTED, never silently skipped ─────────────────────


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([RETIRED], id="bare"),
        pytest.param([f"{RETIRED}=verifier offline"], id="with-reason"),
        pytest.param([f"{RETIRED}="], id="empty-reason"),
        pytest.param(["--reason=x", RETIRED, "--class=regression"], id="mid-argv"),
    ],
)
def test_retired_force_close_is_rejected(argv: list[str]) -> None:
    """The retired flag must raise, and the message must name the new spelling.

    This is the guard against the silent-skip failure mode: `_parse_flags` ignores unknown
    tokens, so WITHOUT the explicit rejecting branch every one of these argvs would parse
    cleanly with `force_reason is None` and the close would proceed un-forced.
    """
    with pytest.raises(CommandError) as excinfo:
        _parse_flags(argv)
    message = excinfo.value.message
    assert "--force" in message
    assert RETIRED in message, "the error must name the flag the caller actually typed"
    assert excinfo.value.returncode == 1


def test_retired_flag_is_not_silently_skipped() -> None:
    """Pin the *mechanism*, not just the message: unknown tokens ARE silently skipped.

    A genuinely unknown flag parses fine (proving the loop's skip behaviour is intact and
    is what would have swallowed the retired flag), while the retired flag raises.
    """
    assert _force_reason(["--not-a-real-flag=x"]) is None  # silently skipped, no raise
    with pytest.raises(CommandError):
        _parse_flags([RETIRED + "=x"])


def test_truncations_are_not_abbreviation_matched() -> None:
    """No prefix/abbreviation matching: a truncated flag must not bind to `--force`.

    `transition`'s loop compares tokens exactly, so `--force-c` is an unknown token — it
    must NOT be treated as `--force` (which would silently bypass a gate) and it need not
    raise. Ticket 424f pursues the same guarantee for the argparse-based commands via
    `allow_abbrev=False`; this asserts the hand-rolled loop already has it.
    """
    assert _force_reason(["--force-c"]) is None
    assert _force_reason(["--forc"]) is None


# ── The retired spelling is gone from the TREE, not just from the parser ─────────

REPO_ROOT = Path(__file__).resolve().parents[2]

# Exempt exact tracked paths only. A new file, even in a listed directory,
# must justify its own use of the retired spelling.
EXEMPT = {
    # The Cloud ADF scrub maps captured old flags to the current spelling.
    # Removing that map would restore the retired spelling in its fixture.
    "scripts/build_cloud_adf_corpus.py",
    # The DC wiki scrub also maps captured old flags to the current spelling.
    "scripts/build_dc_wiki_corpus.py",
    # Frozen LLM-experiment corpora. Their content is recorded experiment INPUT; rewriting
    # them would corrupt the record the experiments' conclusions rest on.
    "docs/experiments/plan-review-gate/runs/e6_selfconsistency_inputs.jsonl",
    "docs/experiments/plan-review-gate/runs/exp2_agentic.jsonl",
    "docs/experiments/plan-review-gate/runs/operator_attested_ac_corpus.jsonl",
    # This mined fixture records review-sidecar plan text describing the 24f7
    # rename. Changing the old flag in that frozen input corrupts its score.
    "docs/experiments/plan-review-gate/fixtures/eval_specs/plan-review-T6.eval.yaml",
    # Historical records that must name the OLD spelling to describe the break itself.
    "CHANGELOG.md",
    "docs/release-notes.md",
    # This file's module docstring, which explains what was retired and why.
    "tests/unit/test_transition_force_flag_24f7.py",
}


def _tracked_files() -> list[str]:
    """Return every git-tracked repo-relative path, including future directories.
    A manually checked rename missed a later script using the retired flag.
    This open-tree scan avoids a stale directory list."""
    out = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [p for p in out.split("\0") if p]


def test_retired_spelling_is_absent_from_every_tracked_file() -> None:
    """Parser tests prove stale flags fail loudly. This tree-wide check finds
    stale callers in tracked scripts that construct rebar argv, including
    production-only canary paths outside typechecked Python."""
    needle = RETIRED.encode()
    offenders = []
    for rel in _tracked_files():
        path = REPO_ROOT / rel
        if rel in EXEMPT or not path.is_file():
            continue
        if needle in path.read_bytes():
            offenders.append(rel)
    assert offenders == [], (
        f"tracked file(s) still use the retired {RETIRED} flag: {offenders}. "
        f'It was renamed to --force[=<reason>] (ticket 24f7) — emit --force="<reason>".'
    )


def test_every_exemption_is_still_earned() -> None:
    """An exemption that no longer applies must be DELETED, not left to rot.

    A stale entry silently widens the guard's blind spot — exactly the failure mode being
    fixed here — so each exempt path must exist and must actually still contain the string.
    """
    needle = RETIRED.encode()
    stale = [
        rel
        for rel in sorted(EXEMPT)
        if not (REPO_ROOT / rel).is_file() or needle not in (REPO_ROOT / rel).read_bytes()
    ]
    assert stale == [], f"exemptions no longer needed — remove them: {stale}"
