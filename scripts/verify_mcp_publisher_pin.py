#!/usr/bin/env python3
"""Verify the mcp-publisher pin embedded in .github/workflows/release.yml (story 08a8).

The release workflow's `mcp_verify` job downloads a specific mcp-publisher tarball and
checks it against a hard-coded SHA-256 before the OIDC-privileged `mcp_registry` job ever
executes the binary. If upstream ever silently re-cut that release (or the pin drifts from
the artifact it names), the digest check in CI would fail the release. This script is the
maintainer-facing pre-flight for exactly that: it parses the pinned URL + SHA-256 out of
release.yml, downloads the URL now, recomputes the digest, and asserts they match — so the
pin is proven correct BEFORE a release run depends on it.

Run: `make verify-mcp-pin` (or `python scripts/verify_mcp_publisher_pin.py`).
Exit 0 when the live download's SHA-256 equals the pinned value; non-zero with a diagnostic
otherwise.
"""

from __future__ import annotations

import hashlib
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / ".github" / "workflows" / "release.yml"

_URL_RE = re.compile(r'MCP_PUBLISHER_URL:\s*["\']?(\S+?)["\']?\s*$', re.MULTILINE)
_SHA_RE = re.compile(r'MCP_PUBLISHER_SHA256:\s*["\']?([0-9a-fA-F]{64})["\']?', re.MULTILINE)


def _extract_pin(text: str) -> tuple[str, str]:
    url_m = _URL_RE.search(text)
    sha_m = _SHA_RE.search(text)
    if not url_m:
        raise SystemExit("verify-mcp-pin: could not find MCP_PUBLISHER_URL in release.yml")
    if not sha_m:
        raise SystemExit("verify-mcp-pin: could not find MCP_PUBLISHER_SHA256 in release.yml")
    return url_m.group(1), sha_m.group(1).lower()


def main() -> int:
    text = RELEASE.read_text(encoding="utf-8")
    url, expected = _extract_pin(text)
    print(f"verify-mcp-pin: pinned URL    = {url}")
    print(f"verify-mcp-pin: pinned SHA256 = {expected}")
    print("verify-mcp-pin: downloading and recomputing the digest…")
    with urllib.request.urlopen(url) as resp:  # noqa: S310 - fixed https GitHub release URL
        data = resp.read()
    actual = hashlib.sha256(data).hexdigest()
    print(f"verify-mcp-pin: downloaded    = {actual} ({len(data)} bytes)")
    if actual != expected:
        print(
            "verify-mcp-pin: MISMATCH — the pinned SHA-256 does not match the live download.\n"
            f"  pinned:     {expected}\n"
            f"  downloaded: {actual}\n"
            "Update the MCP_PUBLISHER_URL/MCP_PUBLISHER_SHA256 pin in release.yml (and the\n"
            "docs/releasing.md Pinning section) only after confirming the new artifact.",
            file=sys.stderr,
        )
        return 1
    print("verify-mcp-pin: OK — the pinned digest matches the live download.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
