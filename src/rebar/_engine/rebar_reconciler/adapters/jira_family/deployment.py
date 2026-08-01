"""Derive a `RemoteRef.instance` string from a deployment's base URL (ticket 6a91, epic e369).

`RemoteRef.instance` names the concrete deployment so two instances of the SAME vendor do not
collide. (`vendor` already separates Cloud from Data Center — Cloud's backend is ``"jira"``, DC's
is ``"jira-datacenter"`` — so what `instance` disambiguates is, say, two DC deployments.)

WHAT THIS DOES **NOT** DO, stated here because `RemoteRef`'s own docstring used to imply otherwise:
it does not prevent LOCAL-ID collision between two same-vendor deployments.
``inbound_translate._jira_key_to_local_id`` is ``"jira-" + jira_key.lower()`` — it derives the id
from the Jira key and nothing else — so two DC deployments that each own a project ``DIG`` both
mint ``jira-dig-123``, whatever `instance` says. Nothing consults `instance` when a local id is
minted. Making the id instance-aware would change the id scheme for every existing Jira-sourced
ticket, a breaking store-wide migration that is deliberately out of scope here.

WHY THE BASE URL, AND WHAT HAPPENS WHEN IT CHANGES. It is available wherever a backend is built,
needs no extra REST round-trip and no operator action. It is also MUTABLE: an operator who moves
their instance changes it. That is tolerable for exactly one reason — **nothing persists a
`RemoteRef`**. It is an in-memory value returned by a port member, so a URL change re-labels
nothing on disk. Any future story that persists one inherits the stability problem and must solve
it there (an operator-set opaque id, or the instance's own server id, are the candidates; neither
is needed for in-memory use and adding either now would be unused configuration).
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
