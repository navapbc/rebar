"""Held-out seam-clean proof for RP-04 C3b (store/snapshot/io cutover).

Observable contract, not internal structure: once the store/snapshot/io subsystem's
below-seam ambient config/credential reads are cut to the approved seam (routed through
an owned ``config.py`` resolver) or annotated as bounded owned-in-place env-control
(``# read-via:`` markers), the config-ownership gate
(``scripts/check_config_ownership.py``) must report **zero** findings for the ten source
files this slice owns, AND their ``LEGACY_EXCEPTIONS`` entries must be gone.

The bounded owned-in-place allowlist is CAPPED and enumerated: exactly three logical
reads stay in place, and because the gate flags each accessed LINE separately they cost
exactly FIVE ``# read-via:`` marked lines, confined to ``_store/hlc.py`` (2) and
``_io/import_ndjson.py`` (3). Every other owned file must carry ZERO markers — so an
implementer cannot satisfy the gate by blanket-marking reads instead of cutting them.

RED before the cutover: the eighteen ``LEGACY_EXCEPTIONS`` entries for these files are
present (registry assertion fails), the reads are still ambient below the seam (gate
assertion fails), and no ``# read-via:`` markers exist yet (marker assertion fails).
GREEN requires the cut/marker split exactly as the slice specifies.
"""

from __future__ import annotations

import re

# Bare-name imports: ``tests/scripts/conftest.py`` puts repo-root ``scripts/`` on
# sys.path (the CI-proven pattern; a ``scripts.`` package prefix does not resolve under
# the full-suite import mode).
import check_config_ownership as gate
import config_ownership_exceptions as exceptions

# The ten files this slice owns, as paths relative to ``src/rebar/`` (the form the gate
# emits and the exception registry stores).
_OWNED_FILES = (
    "_io/import_ndjson.py",
    "_snapshot/git_fetch.py",
    "_snapshot/janitor.py",
    "_snapshot/repo_snapshot.py",
    "_store/ensures.py",
    "_store/env_identity.py",
    "_store/hlc.py",
    "_store/lock.py",
    "_store/project_ensures.py",
    "_store/push.py",
)

# The enumerated owned-in-place allowlist: file -> exact number of ``# read-via:`` marked
# lines it may carry. Everything else must be ZERO.
_MARKER_BUDGET = {
    "_store/hlc.py": 2,  # REBAR_HLC (L61) kill-switch + REBAR_HLC_NOW (L71) test injection
    "_io/import_ndjson.py": 3,  # REBAR_SYNC_PUSH env-control across L216/L217/L395
}
_TOTAL_MARKER_CAP = 5

_MARKER_RE = re.compile(r"#\s*read-via:")
_SRC = gate.REPO_ROOT / "src" / "rebar"


def _gate_findings_for_owned() -> list[str]:
    return [f for f in gate.check(_SRC) if any(name in f for name in _OWNED_FILES)]


def _marker_count(relpath: str) -> int:
    text = (_SRC / relpath).read_text(encoding="utf-8")
    return sum(1 for line in text.splitlines() if _MARKER_RE.search(line))


def test_no_legacy_exceptions_remain_for_owned_files() -> None:
    remaining = [
        (e["path"], e["symbol"]) for e in exceptions.LEGACY_EXCEPTIONS if e["path"] in _OWNED_FILES
    ]
    assert remaining == [], (
        "C3b cutover must remove every LEGACY_EXCEPTIONS entry for the store/snapshot/io "
        f"files; still present: {remaining}"
    )


def test_gate_reports_no_findings_for_owned_files() -> None:
    findings = _gate_findings_for_owned()
    assert findings == [], (
        "config-ownership gate must report zero findings for the store/snapshot/io files "
        f"after the cutover; got: {findings}"
    )


def test_no_path_glob_exception_masks_the_owned_files() -> None:
    globbed = [
        e["path"]
        for e in exceptions.LEGACY_EXCEPTIONS
        if any(ch in str(e["path"]) for ch in "*?[]")
        and any(name in str(e["path"]) for name in _OWNED_FILES)
    ]
    assert globbed == [], f"no path-glob exception may mask the owned files; got: {globbed}"


def test_read_via_markers_are_bounded_and_confined() -> None:
    per_file = {rel: _marker_count(rel) for rel in _OWNED_FILES}
    # Every file not in the budget must carry zero markers (no blanket-marking).
    unexpected = {rel: n for rel, n in per_file.items() if n and rel not in _MARKER_BUDGET}
    assert unexpected == {}, (
        "owned-in-place markers are confined to the enumerated allowlist; unexpected "
        f"markers in: {unexpected}"
    )
    # The budgeted files must carry EXACTLY their allotment.
    wrong = {
        rel: (per_file[rel], want) for rel, want in _MARKER_BUDGET.items() if per_file[rel] != want
    }
    assert wrong == {}, (
        "each allowlisted file must carry exactly its marked-line budget "
        f"(got (actual, want)): {wrong}"
    )
    total = sum(per_file.values())
    assert total == _TOTAL_MARKER_CAP, (
        f"the owned-in-place allowlist caps at {_TOTAL_MARKER_CAP} marked lines; got {total}"
    )
