"""The undeclared-paths refusal must name re-attestation as a required step (bug fb8b).

`_check_file_impact_vs_diff` refuses a close when a linked commit touches a path the
recorded ``file_impact`` does not declare, and advises:

    Declare them with `rebar set-file-impact <ticket> ...` and retry

Following that advice widens ``file_impact``, which changes the material basis hash
(``rebar.llm.plan_review.material_diff.material_basis`` folds ``file_impact`` into the
signed basis), so the *next* close is refused again — this time as ``stale-material``.
The advertised "retry" therefore cannot succeed on its own.

Both refusals are CORRECT and this file does not weaken either. ``file_impact`` defines
the attestation's scope (``rebar.llm.plan_review.read_set`` builds the read set "from the
ticket's declared ``file_impact`` ∪ the files the review CITED"), so a widening genuinely
does invalidate the signature — ``docs/plan-review-gate.md`` states the gate exists so it
"is not bypassable by editing after signing". The defect is only that the first message
under-describes the remedy, sending the author into a loop that reads as a contradiction.

This is the same class `db1c` closed for the bounded-recovery message, whose docstring
records the identical shape: "The advertised remedy for one close gate is a guaranteed
failure of the other, and the message says nothing about that trade."

What this pins:

* the message names the re-attestation requirement (that widening ``file_impact`` is a
  material plan change and ``rebar review-plan`` must be re-run);
* the message still carries the diagnosis it already had — the commit sha, the exact
  undeclared paths, the ``set-file-impact`` remedy and the ``--force`` override — so a
  rewrite of the sentence cannot silently cost the operator the information.
"""

from __future__ import annotations

import pytest

from rebar._commands import close_precheck as _cp
from rebar._commands._seam import CommandError

pytestmark = pytest.mark.unit

_SHA = "0123456789abcdef0123456789abcdef01234567"
_DECLARED = "src/rebar/declared.py"
_UNDECLARED = "src/rebar/_guides/criterion-pins.json"


@pytest.fixture
def _refusal(monkeypatch) -> CommandError:
    """Drive the real refusal: a linked commit touching one undeclared path."""
    from rebar._engine_support import commit_impact

    monkeypatch.setattr(_cp, "_union_file_impact", lambda ids, tracker: [_DECLARED])
    monkeypatch.setattr(_cp, "_attached_commit_shas", lambda ids, tracker: [_SHA])
    monkeypatch.setattr(commit_impact, "is_merge_commit", lambda sha, root: False)
    monkeypatch.setattr(commit_impact, "changed_paths", lambda sha, root: [_DECLARED, _UNDECLARED])
    with pytest.raises(CommandError) as exc:
        _cp._check_file_impact_vs_diff({"fb8b-189d-97ab-4546"}, [], "/tracker", "/code")
    # Precondition: we actually drove the undeclared-paths branch, not some other refusal.
    assert _UNDECLARED in exc.value.message, "fixture did not reach the undeclared-paths refusal"
    return exc.value


def test_message_names_the_reattestation_requirement(_refusal: CommandError) -> None:
    """The operator must learn that declaring the paths invalidates the attestation.

    Without this, `set-file-impact` + retry looks like a two-step remedy and the resulting
    `stale-material` refusal reads as the gates contradicting each other.
    """
    msg = _refusal.message
    assert "review-plan" in msg, (
        "the refusal must name `rebar review-plan` as a required step — declaring the "
        "paths widens file_impact, which stales the attestation, so a bare retry fails. "
        f"Got: {msg!r}"
    )
    assert "material" in msg.lower(), (
        "the refusal must say the declaration is a MATERIAL plan change, so the operator "
        f"can connect it to the `stale-material` refusal they are about to hit. Got: {msg!r}"
    )


def test_message_keeps_its_existing_diagnosis(_refusal: CommandError) -> None:
    """Adding the re-attestation sentence must not cost the operator the
    diagnosis the message already carried."""
    msg = _refusal.message
    assert _SHA in msg, "the offending commit sha must still be named"
    assert _UNDECLARED in msg, "the exact undeclared path must still be named"
    assert "set-file-impact" in msg, "the set-file-impact remedy must still be named"
    assert "--force" in msg, "the --force override must still be offered"
