"""Derive a normalized ``RemoteRef.instance`` label from a deployment base URL.

The label exists only in memory and changes when the URL changes. It distinguishes
same-vendor deployments within ``RemoteRef`` but does not prevent collisions in
the current local-ID scheme. Persisting it would require a separate stability
design.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": "80", "https": "443"}


def instance_from_base_url(base_url: str) -> str:
    """Normalise ``base_url`` to a stable `instance` label, or ``""`` if it is unusable.

    Normalisation matters more than the exact format: two SPELLINGS of one deployment must agree,
    or the same instance looks like two. So the scheme is dropped (http vs https is not a different
    deployment), the host is lower-cased (DNS is case-insensitive), a DEFAULT port is dropped while
    a non-default one is kept (``:8080`` genuinely distinguishes), and a trailing slash is removed.

    The CONTEXT PATH is RETAINED. Data Center is commonly served under one — the harness itself is
    at ``/jira`` — and two deployments can share a host while differing only there, so dropping it
    would merge them.

    Returns ``""`` for an empty or unparseable URL rather than raising: this feeds an identity
    label, and a backend that cannot name its deployment should degrade to "unnamed" rather than
    make the backend unbuildable.
    """
    if not base_url or not base_url.strip():
        return ""
    parts = urlsplit(base_url.strip())
    host = (parts.hostname or "").lower()
    if not host:
        # No scheme -> urlsplit puts everything in `path`. Retry with one so a bare
        # "jira.example.com/jira" normalises the same as "https://jira.example.com/jira".
        parts = urlsplit(f"//{base_url.strip()}")
        host = (parts.hostname or "").lower()
    if not host:
        return ""
    port = parts.port
    scheme = (parts.scheme or "https").lower()
    if port is not None and str(port) != _DEFAULT_PORTS.get(scheme, ""):
        host = f"{host}:{port}"
    path = (parts.path or "").rstrip("/")
    return f"{host}{path}"
