"""Happy-path seam-clean spec for RP-04 C3f (residual drain).

The nine "residual" source files below own the LAST below-seam ambient config/credential
reads that RP-04's earlier cutovers left on the ``LEGACY_EXCEPTIONS`` whitelist. This slice
disposes every one of them and removes its whitelist row, so the config-ownership gate is
clean for these files WITHOUT their exceptions.

This slice does NOT reach whole-tree EMPTY: the two ``binding_lifecycle.py`` rows remain
(RP-04 S7.3.a's scope). So the spec is a PER-PATH drain of the nine owned files.
"""

from __future__ import annotations

# Bare-name imports: ``tests/scripts/conftest.py`` puts repo-root ``scripts/`` on sys.path.
import check_config_ownership as gate
import config_ownership_exceptions as exceptions

_OWNED_FILES = (
    "_logging.py",
    "_opcert_signing.py",
    "_operation_config.py",
    "mirror_guard.py",
    "signing.py",
    "review_bot/app.py",
    "grounding/harness.py",
    "grounding/oracle.py",
    "grounding/resolve.py",
)

_SRC = gate.REPO_ROOT / "src" / "rebar"


def test_no_legacy_exceptions_remain_for_owned_files() -> None:
    remaining = [
        (e["path"], e["symbol"]) for e in exceptions.LEGACY_EXCEPTIONS if e["path"] in _OWNED_FILES
    ]
    assert remaining == [], (
        "C3f residual drain must remove every LEGACY_EXCEPTIONS entry for the nine owned "
        f"files (per-path, not whole-set EMPTY); still present: {remaining}"
    )


def test_gate_reports_no_findings_for_owned_files() -> None:
    findings = [f for f in gate.check(_SRC) if any(name in f for name in _OWNED_FILES)]
    assert findings == [], (
        "config-ownership gate must report zero findings for the nine owned files after "
        f"the residual drain; got: {findings}"
    )


def test_no_path_glob_exception_masks_the_owned_files() -> None:
    globbed = [
        e["path"]
        for e in exceptions.LEGACY_EXCEPTIONS
        if any(ch in str(e["path"]) for ch in "*?[]")
        and any(name in str(e["path"]) for name in _OWNED_FILES)
    ]
    assert globbed == [], f"no path-glob exception may mask the owned files; got: {globbed}"
