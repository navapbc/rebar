"""Pure environment-projection for child processes rebar spawns (RP-04 S3, ticket 6e3b).

A reconciler that spawns a child process must decide *which* of the parent's environment
variables the child may see. The risky variables are the **send credentials** each rebar
Jira adapter owns — Cloud's ``JIRA_API_TOKEN`` and Data Center's ``JIRA_PAT``. A child
that has no business sending as an adapter must not inherit that adapter's secret from the
ambient environment; an "owning" child must receive its own credential from an *explicit
overlay*, never by ambient inheritance that could silently smuggle a stale or wrong token.

This module is that decision, expressed as a **pure** projection over a mapping:

    project_child_env(base, *, relationship, owner=None, overlay=None) -> dict[str, str]

It returns a brand-new ``dict`` and NEVER mutates ``base`` or the process-global
``os.environ`` — it does not even read ``os.environ``. Callers pass the base mapping they
want projected (typically a snapshot of ``os.environ``) and get a fresh mapping to hand to
the child.

The registry: NAMES, never VALUES
----------------------------------
:data:`_ADAPTER_SECRET_NAMES` is a checked-in registry of the exact env-var **names** each
adapter's send credential lives under. It is deliberately a set of *names* — it carries no
secret material, so it is safe to commit, log, and diff. Non-secret Jira configuration
(``JIRA_URL`` / ``JIRA_USER`` / ``JIRA_PROJECT``) is NOT in the registry: those are plain
config a child may freely inherit, and only the *sending secret* names are stripped.

Relationship semantics
----------------------
``relationship`` names the child's trust relationship to the parent's adapter capability:

- ``"same_capability"`` — a trusted child that runs with the *same* sending capability as
  the parent (e.g. a helper that legitimately sends as the same adapter). It inherits the
  full ambient environment: the result is a copy of ``base`` (with ``overlay`` applied on
  top when one is given).

- ``"owning"`` — a child that OWNS exactly one adapter (named by ``owner``) and must send
  as it. Every adapter-owned secret NAME is first removed from ``base`` (so no ambient
  token leaks in), then the caller's ``overlay`` is applied on top. The owner's own
  credential therefore comes ONLY from the overlay — never inherited from ``base``. All
  non-secret config and unknown native variables (``GIT_*`` / ``SSH_*`` / ``AWS_*`` /
  proxy / CA bundles) survive untouched.

- ``"unrelated"`` — an unrelated sibling with no sending capability at all. Every
  adapter-owned secret NAME is removed and NO overlay is applied. Non-secret config and
  unknown native variables survive.

Fail-closed
-----------
An unknown ``relationship`` raises :class:`ValueError` rather than defaulting to a
permissive projection, and ``"owning"`` requires an ``owner`` (also a :class:`ValueError`
when absent). The safe default when the caller is unclear is to refuse, not to inherit.
"""

from __future__ import annotations

from collections.abc import Mapping

# ---------------------------------------------------------------------------
# The registry: the exact secret env-var NAMES each adapter owns — NAMES ONLY.
#
# Each key is an adapter identifier (as registered in the reconciler backend
# registry); each value is the frozen set of env-var NAMES holding that adapter's
# *send credential*. These are identifiers, never secret values, so this mapping is
# safe to commit and inspect. Non-secret configuration (JIRA_URL / JIRA_USER /
# JIRA_PROJECT) is intentionally absent — only sending secrets are listed.
# ---------------------------------------------------------------------------
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
    """Project ``base`` into a NEW child-process environment mapping.

    Pure: returns a fresh ``dict`` and never mutates ``base``, ``overlay``, or the
    process-global ``os.environ``. See the module docstring for the full contract; the
    ``relationship`` semantics in brief:

    - ``"same_capability"``: a copy of ``base`` with ``overlay`` applied on top.
    - ``"owning"``: ``base`` with every adapter-owned secret NAME removed, then
      ``overlay`` applied on top (so the ``owner``'s secret comes only from the overlay).
      Requires ``owner``.
    - ``"unrelated"``: ``base`` with every adapter-owned secret NAME removed; no overlay.

    :raises ValueError: for an unknown ``relationship``, or an ``"owning"`` projection
        with no ``owner``.
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
