"""Audit page accepts both criterion `kind` values (story a356, ADR 0101) — happy path.

`_completion_section` flags a criterion as `lacking` when its evidence kind is the
out-of-codebase one and the criterion is not met. ADR 0101 renames that kind's value from
`operator-attested` to `non-codebase`; the READER must accept both so the 827 live tickets
holding legacy COMPLETION_VERDICT records keep rendering.
"""

from __future__ import annotations

from rebar.audit.page import _completion_section


def _section(kind: str, met: bool = False) -> dict:
    return _completion_section(
        {
            "sidecar": {
                "verdict": "PASS",
                "criteria": [{"criterion": "the deploy is live", "met": met, "kind": kind}],
            }
        }
    )


def _row(kind: str, met: bool = False) -> dict:
    return _section(kind, met)["criteria"][0]


def test_non_codebase_unmet_is_lacking() -> None:
    """The NEW kind value marks an unmet out-of-codebase criterion as lacking."""
    assert _row("non-codebase")["lacking"] is True


def test_legacy_operator_attested_unmet_is_still_lacking() -> None:
    """The LEGACY value keeps rendering — persisted verdicts are immutable records."""
    assert _row("operator-attested")["lacking"] is True


def test_codebase_verifiable_unmet_is_not_lacking() -> None:
    """`lacking` stays narrow: it flags missing ATTESTATION, not any unmet criterion."""
    assert _row("codebase-verifiable")["lacking"] is False
