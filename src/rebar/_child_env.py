"""Pure child-process environment projection for adapter credentials.

The checked-in registry contains secret variable names, never values; ordinary Jira
configuration and unknown native variables remain inheritable. ``same_capability``
copies the base and applies an overlay. ``owning`` first removes every registered
secret, then applies its explicit overlay so no credential arrives ambiently.
``unrelated`` removes every registered secret and ignores overlays. The function never
reads or mutates ``os.environ`` or its input mappings. Unknown relationships and an
ownerless ``owning`` projection fail closed with :class:`ValueError`.
"""

from __future__ import annotations

from collections.abc import Mapping

# Adapter IDs map only to send-credential variable names. Values and ordinary Jira
# configuration are deliberately absent, so this registry is safe to inspect.
_ADAPTER_SECRET_NAMES: dict[str, frozenset[str]] = {
    # Jira Cloud authenticates with an API token.
    "jira": frozenset({"JIRA_API_TOKEN"}),
    # Jira Data Center authenticates with a Personal Access Token.
    "jira-datacenter": frozenset({"JIRA_PAT"}),
}

_VALID_RELATIONSHIPS = frozenset({"same_capability", "owning", "unrelated"})


def adapter_secret_names(adapter: str) -> frozenset[str]:
    """Return the exact secret env-var NAMES the given ``adapter`` owns.

    An unknown adapter owns no declared secrets, so this returns an empty
    ``frozenset`` rather than raising — the projection strips nothing it does not
    know about, and the caller can still enumerate known adapters via
    :func:`owned_secret_names`.
    """
    return _ADAPTER_SECRET_NAMES.get(adapter, frozenset())


def owned_secret_names() -> frozenset[str]:
    """Return the union of every adapter's declared secret env-var NAMES."""
    names: set[str] = set()
    for adapter_names in _ADAPTER_SECRET_NAMES.values():
        names |= adapter_names
    return frozenset(names)


def project_child_env(
    base: Mapping[str, str],
    *,
    relationship: str,
    owner: str | None = None,
    overlay: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a fresh environment for the requested capability relationship.

    ``same_capability`` copies ``base`` plus ``overlay``; ``owning`` strips all
    registered secrets before applying its explicit overlay; ``unrelated`` strips
    them and ignores overlays. Invalid relationships and ownerless ``owning`` calls
    raise :class:`ValueError`.
    """
    if relationship not in _VALID_RELATIONSHIPS:
        raise ValueError(
            f"unknown child-env relationship {relationship!r}; "
            f"expected one of {sorted(_VALID_RELATIONSHIPS)}"
        )

    if relationship == "same_capability":
        result = dict(base)
        if overlay is not None:
            result.update(overlay)
        return result

    if relationship == "owning" and owner is None:
        raise ValueError("an 'owning' child-env projection requires an 'owner' adapter")

    # Both "owning" and "unrelated" start from base with EVERY adapter-owned secret
    # NAME stripped, so no ambient send credential leaks into the child.
    to_strip = owned_secret_names()
    result = {name: value for name, value in base.items() if name not in to_strip}

    # "owning" then layers the owner's explicit overlay back on top; "unrelated" gets
    # no overlay at all.
    if relationship == "owning" and overlay is not None:
        result.update(overlay)
    return result
