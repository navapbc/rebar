"""Backend-neutrality gate for the reconciler core (epic bbf1 / story S4).

S4 routes the backend-neutral core through the ``Backend`` port instead of naming
Jira concretely. These assertions pin the two unambiguous neutrality wins so a
regression that re-introduces an inline Jira transport construction (or a hard-coded
provider literal) fails CI:

(a) No core module constructs an ``AcliClient(...)`` inline. The transport is now
    obtained from the configured backend (``select_backend(load_config()).transport``)
    via the ``_load_acli`` / ``build_acli_client_from_env`` seams; the ONLY sanctioned
    ``AcliClient(...)`` construction lives in ``adapters/jira/backend.py``'s factory,
    which these files must not duplicate.

(b) ``apply_inbound_records.py`` no longer carries the two provider-identity ``"jira"``
    literals (they now flow from the selected backend's ``vendor``); the sole remaining
    ``"jira"`` token is the deliberately-retained ``validate_creation_channel("jira")``
    creation-channel VOCABULARY key, which is out of scope.

Implemented as a source-text gate (grep-style, reading the files) so it is independent
of import wiring and fails BEFORE the S4 rewiring, PASSES after.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REC = Path(__file__).resolve().parents[3] / "src" / "rebar" / "_engine" / "rebar_reconciler"

# Core modules that must NOT construct an AcliClient inline (the seam returns the
# backend transport instead). adapters/jira/ is deliberately excluded — the factory
# there is the single sanctioned construction site.
_NO_INLINE_ACLICLIENT = (
    "applier.py",
    "fetcher.py",
    "run_differs.py",
    "_attestation.py",
)


@pytest.mark.parametrize("filename", _NO_INLINE_ACLICLIENT)
def test_no_inline_acliclient_construction(filename: str) -> None:
    """The core transport/construction sites route through the Backend port, so no
    ``AcliClient(`` construction (in code, comment, or docstring) remains."""
    path = _REC / filename
    text = path.read_text()
    assert "AcliClient(" not in text, (
        f"{filename} still constructs an AcliClient inline — route it through the "
        f"backend transport (select_backend(load_config()).transport) instead. The "
        f"only sanctioned AcliClient(...) construction lives in adapters/jira/backend.py."
    )


def test_apply_inbound_records_has_single_jira_literal() -> None:
    """``apply_inbound_records.py`` carries exactly one ``"jira"`` literal — the
    retained ``validate_creation_channel("jira")`` vocabulary key. The two former
    provider-identity literals now come from the selected backend's ``vendor``."""
    path = _REC / "apply_inbound_records.py"
    text = path.read_text()
    occurrences = text.count('"jira"')
    assert occurrences == 1, (
        f'expected exactly one "jira" literal in apply_inbound_records.py (the '
        f"validate_creation_channel vocabulary key), found {occurrences}. The two "
        f"provider-identity literals must be replaced by the backend vendor."
    )
    assert 'validate_creation_channel("jira")' in text, (
        'the single retained "jira" literal must be the '
        'validate_creation_channel("jira") vocabulary key'
    )
