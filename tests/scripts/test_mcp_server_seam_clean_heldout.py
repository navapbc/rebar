"""Held-out seam-clean proof for RP-04 S7.3.e (mcp-server cutover).

Observable contract, not internal structure: once the MCP-server subsystem's
below-seam ambient config/credential reads are cut to the approved seam (the
operation snapshot / a startup binding / the provider-credential boundary) or
registered as owned non-legacy entries, the config-ownership gate
(``scripts/check_config_ownership.py``) must report **zero** findings for the two
MCP files, AND their ``LEGACY_EXCEPTIONS`` entries must be gone.

RED before the cutover: the three ``LEGACY_EXCEPTIONS`` entries below are present
(so the registry assertion fails), and removing them without cutting the reads
would make the gate assertion fail instead. GREEN requires both — the reads cut
and the entries removed — which is exactly the slice's outcome.
"""

from __future__ import annotations

# Bare-name imports: ``tests/scripts/conftest.py`` puts repo-root ``scripts/`` on
# sys.path (the CI-proven pattern; a ``scripts.`` package prefix does not resolve
# under the full-suite import mode — those modules also import each other by bare
# name).
import check_config_ownership as gate
import config_ownership_exceptions as exceptions

# The two files this slice owns, as paths relative to ``src/rebar/`` (the form the
# gate emits and the exception registry stores).
_MCP_FILES = ("mcp_server.py", "_mcp_auth.py")


def _mcp_gate_findings() -> list[str]:
    """Findings the real gate (default scan root ``src/rebar``) attributes to the
    MCP files."""
    scan_root = gate.REPO_ROOT / "src" / "rebar"
    return [f for f in gate.check(scan_root) if any(name in f for name in _MCP_FILES)]


def test_no_legacy_exceptions_remain_for_mcp_files() -> None:
    remaining = [
        (e["path"], e["symbol"]) for e in exceptions.LEGACY_EXCEPTIONS if e["path"] in _MCP_FILES
    ]
    assert remaining == [], (
        "MCP-server cutover must remove every LEGACY_EXCEPTIONS entry for "
        f"{_MCP_FILES}; still present: {remaining}"
    )


def test_gate_reports_no_findings_for_mcp_files() -> None:
    findings = _mcp_gate_findings()
    assert findings == [], (
        "config-ownership gate must report zero findings for the MCP files after "
        f"the cutover; got: {findings}"
    )


def test_no_path_glob_exception_masks_the_mcp_files() -> None:
    # AC forbids satisfying the gate via a path-glob wildcard exception.
    globbed = [
        e["path"]
        for e in exceptions.LEGACY_EXCEPTIONS
        if any(ch in str(e["path"]) for ch in "*?[]")
        and any(name in str(e["path"]) for name in _MCP_FILES)
    ]
    assert globbed == [], f"no path-glob exception may mask the MCP files; got: {globbed}"
