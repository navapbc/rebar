"""Version-sync guard (ticket 9f40 / adjoining-unheeding-hornshark, doc-drift sweep D2).

`docs/api-stability.md` states the current release version in prose ("currently X.Y.Z").
That marker silently drifted behind `pyproject.toml` (it read 0.6.0 while the package was
0.7.1). This test mirrors a CI gate: it fails whenever the documented version diverges from
the authoritative `pyproject.toml` version, so the doc can never fall stale again without
turning the build red. It is a unit test (not a bespoke workflow step) so it runs on the
change's own tree under `make test` — gating both the branch CI and the Gerrit Verified vote
with no workflow-bootstrap lag.
"""

from __future__ import annotations

import re
from pathlib import Path

import tomllib

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
API_STABILITY = REPO_ROOT / "docs" / "api-stability.md"

# Matches the prose marker: "... (currently 0.7.1). ...".
_DOC_VERSION_RE = re.compile(r"currently\s+(\d+\.\d+\.\d+)")


def _pyproject_version() -> str:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]


def _doc_version() -> str:
    m = _DOC_VERSION_RE.search(API_STABILITY.read_text(encoding="utf-8"))
    assert m, (
        f"could not find a 'currently <X.Y.Z>' version marker in {API_STABILITY.name}; "
        "the version-sync guard relies on that exact prose — keep it so this check stays live"
    )
    return m.group(1)


def test_api_stability_doc_version_matches_pyproject() -> None:
    doc, pkg = _doc_version(), _pyproject_version()
    assert doc == pkg, (
        f"{API_STABILITY.name} says the version is {doc!r} but pyproject.toml is {pkg!r}.\n"
        f"Update the 'currently {doc}' marker in {API_STABILITY.name} to {pkg}."
    )
