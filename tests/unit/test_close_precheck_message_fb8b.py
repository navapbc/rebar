"""When a commit touches undeclared paths, the close refusal must name those paths, commit,
file-impact update, and required plan re-attestation. Widening file impact changes signed
material, so merely retrying cannot satisfy the next close gate. Retain the force override
information."""

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
