"""Shared ticket-ID resolution primitives (stdlib-only leaf).

The single resolution seam Python CLIs and the library use, so every surface
accepts the same ID forms. The forms to WRITE are the alias (preferred), the
8-hex two-quad short id, the full 16-hex canonical id, and the Jira issue key
(e.g. ``REB-310``).

Shorter unique prefixes (down to 4 characters) still resolve, but a bare
single-quad 4-hex prefix is DEPRECATED as a reference form and should not be
used in new prose, docs, or tooling: with a large store those fragments collide
constantly, so they resolve ambiguously to nothing and, worse, turn any text
that merely CONTAINS one into an accidental ticket citation. Resolution of
existing short forms is unchanged — this is compatibility behavior, not a
recommendation. Scanners that resolve candidates the user never supplied should
pass ``quiet=True`` (see :func:`resolve_ticket_id`).

This is a **top-of-tree leaf**: it imports only stdlib + ``rebar._alias`` (itself
a stdlib-only leaf) and NOTHING from ``rebar.reducer`` / ``rebar._engine_support``
/ ``rebar._commands`` / ``rebar.llm``.  It therefore sits BELOW both the pure
event-replay layer (``rebar.reducer``) and the higher read layer
(``rebar._engine_support``), so both can depend on it downward without a package
cycle — the same pattern ``rebar._alias`` uses.  Historically this lived in
``rebar._engine_support.resolver``, which forced the reducer to reach UP into
``_engine_support`` via a function-local import (a layering inversion + import
cycle); moving the primitive here removes that back-edge.  ``rebar._engine_support
.resolver`` now re-exports these names, so its public surface is unchanged.

Alias lookup is done IN-PROCESS (Tier E E6.5a — replacing the
``ticket-alias-resolve.py`` subprocess): the alias scan reads each ticket's
CREATE event (and the latest SNAPSHOT, for compacted tickets) and matches a
stored ``data.alias`` or a backfilled ``compute_alias`` — the same single-source
alias helper (``rebar._alias``) the create path uses, so stored-at-create and
backfilled-at-resolve aliases stay in lock-step.

Jira-key lookup consults the reconciler's **binding store** reverse index
(``.tickets-tracker/.bridge_state/bindings.json`` → ``reverse: {jira_key →
local_id}``), which is the authoritative Jira↔rebar mapping. (Historically the
resolver scanned ``data.jira_key`` on CREATE/SNAPSHOT events, but that field is
never written — the live mapping is the binding store — so that path was dead and
has been replaced.)
"""

from __future__ import annotations

import bisect
import json
import os
import re
import sys
from typing import NamedTuple

from rebar._alias import compute_alias

_FULL_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$")
_SHORT_ID_RE = re.compile(r"^[a-z0-9]{4}-[a-z0-9]{4}$")
# Jira issue key shape: project key (>=2 alnum, leading letter) + "-" + number,
# e.g. ``REB-310``. Disjoint from lowercase-hex full/short IDs and lowercase-word
# aliases, so a Jira-key match never collides with the other forms.
_JIRA_KEY_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]+-[0-9]+$")


def _is_safe_segment(name: str) -> bool:
    """True iff ``name`` is a single, safe path segment — the shape every valid
    ticket id / alias / Jira key / prefix takes — so joining it to the tracker
    directory cannot escape it.

    Rejects the empty string, ``.`` / ``..``, any name carrying a path separator
    (``/``, ``\\``, or the OS ``altsep``), a NUL byte, or a leading dot (dotfiles
    such as ``.bridge_state``). This is the source-side path-injection guard used
    by :func:`_existing_ticket_dir_name`: an id that fails it never reaches a
    filesystem join built from untrusted input.
    """
    if not name or name in (".", ".."):
        return False
    if "\x00" in name or name[0] == ".":
        return False
    if "/" in name or "\\" in name:
        return False
    if os.sep in name or (os.altsep and os.altsep in name):
        return False
    # A safe segment is its own basename and has no directory component.
    return os.path.basename(name) == name and not os.path.dirname(name)


def _existing_ticket_dir_name(tracker_dir: str, name: str) -> str | None:
    """Return ``name``'s directory basename iff it is a safe segment that names an
    existing directory **contained within** ``tracker_dir``; else ``None``.

    The ``normpath`` + prefix containment check is a path-injection barrier, and
    the returned :func:`os.path.basename` is provably free of directory components
    — so the resolver's callers can join the result to ``tracker_dir`` without
    escaping it. A traversing / absolute ``name`` fails the safe-segment guard or
    the containment check and yields ``None``.
    """
    if not _is_safe_segment(name):
        return None
    tracker_norm = os.path.normpath(tracker_dir)
    candidate = os.path.normpath(os.path.join(tracker_norm, name))
    # Containment barrier: only a normalized candidate that is a CHILD of the
    # tracker directory is accepted. A traversing/absolute ``name`` normalizes
    # outside and fails ``startswith``, yielding None. The plain normpath +
    # ``startswith`` form (no extra disjunct) is exactly CodeQL's recognized
    # path-injection sanitizer (PathNormalization + SafeAccessCheck), so every
    # sink fed by this function's return value is seen as sanitized. A real ticket
    # id is always a child of the tracker, so this never rejects a valid id.
    if not candidate.startswith(tracker_norm + os.sep):
        return None
    if os.path.isdir(candidate):
        return os.path.basename(candidate)
    return None


def _load_binding_reverse(tracker_dir: str) -> dict:
    """Load the binding store's reverse index ``{jira_key → local_id}``, or ``{}``.

    The single reader of ``<tracker_dir>/.bridge_state/bindings.json`` →
    ``reverse``. Best-effort: a missing/corrupt store or a non-dict ``reverse``
    yields ``{}`` (Jira mapping simply unavailable — never raises). Shared by
    :func:`_resolve_via_binding_store` (id resolution) and
    :func:`binding_jira_key_map` (search enrichment) so both read the mapping the
    same way.
    """
    bindings_path = os.path.join(tracker_dir, ".bridge_state", "bindings.json")
    try:
        with open(bindings_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    reverse = data.get("reverse")
    return reverse if isinstance(reverse, dict) else {}


def binding_jira_key_map(tracker_dir: str) -> dict[str, str]:
    """Map ``local_id → jira_key`` for every binding, inverting the reverse index.

    The read-side counterpart of :func:`_resolve_via_binding_store`: it exposes
    the SAME authoritative binding store (``reverse: {jira_key → local_id}``) as a
    ``ticket_id → jira_key`` lookup, so a caller can annotate reduced states with
    their bound Jira key in one pass. Best-effort and never raises. If two Jira
    keys somehow map to one local id, the first wins (``setdefault``).
    """
    out: dict[str, str] = {}
    for jira_key, local_id in _load_binding_reverse(tracker_dir).items():
        if isinstance(jira_key, str) and jira_key and isinstance(local_id, str) and local_id:
            out.setdefault(local_id, jira_key)
    return out


def _resolve_via_binding_store(target: str, tracker_dir: str) -> str | None:
    """Resolve a Jira issue key to its bound local ticket-dir name, or None.

    Consults the reconciler's binding store reverse index
    (``<tracker_dir>/.bridge_state/bindings.json`` → ``reverse: {jira_key →
    local_id}``) — the authoritative Jira↔rebar mapping. Best-effort: a missing or
    corrupt store, a non-dict ``reverse``, an unbound key, or a binding that points
    at a ticket dir that no longer exists all yield None (Jira resolution simply
    unavailable — this never raises). The lookup tries the key verbatim then
    upper-cased, since Jira project keys are canonically upper-case.
    """
    reverse = _load_binding_reverse(tracker_dir)
    if not reverse:
        return None
    local_id = reverse.get(target) or reverse.get(target.upper())
    if not isinstance(local_id, str) or not local_id:
        return None
    if os.path.isdir(os.path.join(tracker_dir, local_id)):
        return local_id
    return None


def _effective_alias_for_dir(tracker_dir: str, name: str) -> str:
    """Return the effective alias for a single ticket directory ``name``.

    A ticket's effective alias is its stored ``data.alias`` (CREATE event, or the
    latest non-PRECONDITIONS SNAPSHOT ``compiled_state`` for compacted tickets) or
    a backfilled ``compute_alias`` — keeping stored-at-create and
    backfilled-at-resolve aliases in lock-step. Returns ``""`` for a dotfile, a
    non-directory, or on a per-ticket I/O error (an unreadable ticket is skipped,
    never a hard failure).
    """
    if name.startswith("."):
        return ""
    ticket_dir = os.path.join(tracker_dir, name)
    if not os.path.isdir(ticket_dir):
        return ""
    # First CREATE (lexically earliest) + latest non-PRECONDITIONS SNAPSHOT
    # (compacted tickets fold the CREATE into a SNAPSHOT compiled_state).
    create_path = None
    snapshot_path = None
    try:
        for fname in sorted(os.listdir(ticket_dir)):
            if fname.endswith("-CREATE.json") and create_path is None:
                create_path = os.path.join(ticket_dir, fname)
            elif fname.endswith("-SNAPSHOT.json") and not fname.endswith(
                "-PRECONDITIONS-SNAPSHOT.json"
            ):
                snapshot_path = os.path.join(ticket_dir, fname)
    except OSError:
        return ""
    stored_alias = ""
    if create_path:
        try:
            with open(create_path, encoding="utf-8") as f:
                data = json.load(f).get("data", {}) or {}
            stored_alias = data.get("alias") or ""
        except (OSError, json.JSONDecodeError):
            pass
    # SNAPSHOT compiled_state is authoritative for compacted tickets; fill the
    # missing alias BEFORE the compute_alias backfill (wordlist drift).
    if snapshot_path and not stored_alias:
        try:
            with open(snapshot_path, encoding="utf-8") as f:
                snap_state = (json.load(f).get("data", {}) or {}).get("compiled_state", {}) or {}
            stored_alias = snap_state.get("alias") or ""
        except (OSError, json.JSONDecodeError):
            pass
    return stored_alias or compute_alias(name) or ""


class ResolverScanIndex(NamedTuple):
    """Everything a many-id resolution needs from ONE pass over the tracker root.

    ``alias_to_dirs`` — ``{effective alias -> [ticket-dir names]}`` for the alias
    branch. ``sorted_dir_names`` — the sorted list of non-dot ticket directory
    names, so the 8-hex short-id and generic-prefix branches can prefix-match by
    :func:`bisect` instead of re-listing the tracker root per candidate. Both are
    built together so a scanner (e.g. the close precheck's walk over every
    historical commit reference) pays ONE store listing regardless of how many
    distinct candidates history references.
    """

    alias_to_dirs: dict[str, list[str]]
    sorted_dir_names: list[str]


def _scan_tracker_root(tracker_dir: str) -> ResolverScanIndex | None:
    """Single pass over the tracker root building both resolution indexes.

    Returns ``None`` on a hard failure listing the tracker (so callers fall back
    to per-call scanning), matching :func:`_scan_alias`'s contract that a hard
    failure never masquerades as "no match". Dotfiles (e.g. ``.bridge_state``) and
    non-directory entries are excluded, mirroring the per-candidate scan filters.
    """
    try:
        entries = os.listdir(tracker_dir)
    except OSError as exc:
        print(f"Error: cannot list {tracker_dir!r}: {exc}", file=sys.stderr)
        return None
    alias_to_dirs: dict[str, list[str]] = {}
    dir_names: list[str] = []
    for name in entries:
        if name.startswith(".") or not os.path.isdir(os.path.join(tracker_dir, name)):
            continue
        dir_names.append(name)
        effective_alias = _effective_alias_for_dir(tracker_dir, name)
        if effective_alias:
            alias_to_dirs.setdefault(effective_alias, []).append(name)
    dir_names.sort()
    return ResolverScanIndex(alias_to_dirs=alias_to_dirs, sorted_dir_names=dir_names)


def build_resolver_scan_index(tracker_dir: str) -> ResolverScanIndex | None:
    """Build the combined alias + directory-name index in ONE tracker-root pass.

    A caller that must resolve MANY ids against the same store (the close
    precheck's walk over every historical commit reference) builds this once and
    threads it into :func:`resolve_ticket_id` via ``alias_index=`` and
    ``dir_names=`` — turning an O(unique_candidates x store) sequence of full-store
    listings (alias AND short-id/prefix branches) into one pass plus in-memory
    lookups. Returns ``None`` on a hard failure listing the tracker, so the caller
    can fall back to per-call scanning.
    """
    return _scan_tracker_root(tracker_dir)


def build_alias_index(tracker_dir: str) -> dict[str, list[str]] | None:
    """Build ``{alias -> [ticket-dir names]}`` in a single pass over the tracker.

    A caller that must resolve MANY aliases against the same store (e.g. the close
    precheck's walk over every historical commit reference) builds this once and
    threads it into :func:`resolve_ticket_id` via ``alias_index=`` — turning an
    O(distinct_aliases x store) sequence of full-store scans into one pass plus
    in-memory lookups. Returns ``None`` on a hard failure listing the tracker (so
    the caller can fall back to per-call scanning), matching :func:`_scan_alias`'s
    contract that a hard failure never masquerades as "no match".

    This is the alias-only view of :func:`build_resolver_scan_index`; a caller that
    also resolves short-id/prefix candidates should build the combined index once.
    """
    scanned = _scan_tracker_root(tracker_dir)
    return None if scanned is None else scanned.alias_to_dirs


def _dir_prefix_matches(sorted_names: list[str], prefix: str) -> list[str]:
    """Ticket-dir names in ``sorted_names`` that start with ``prefix``.

    ``sorted_names`` is pre-sorted, so the matching names form one contiguous run
    found by :func:`bisect` — O(log store + matches) instead of a full scan. Used
    to resolve short-id and generic-prefix candidates against a prebuilt index
    without re-listing the tracker root.
    """
    start = bisect.bisect_left(sorted_names, prefix)
    matches: list[str] = []
    for name in sorted_names[start:]:
        if name.startswith(prefix):
            matches.append(name)
        else:
            break
    return matches


def _scan_alias(target: str, tracker_dir: str) -> list[str] | None:
    """Scan tickets for an alias matching ``target`` (in-process alias resolution).

    Returns the list of matching ticket-dir names, or ``None`` on a hard failure
    listing the tracker (a hard failure must not masquerade as "no match").
    Per-ticket I/O errors skip that ticket. Each ticket's effective alias is
    computed by :func:`_effective_alias_for_dir`.
    """
    try:
        entries = sorted(os.listdir(tracker_dir))
    except OSError as exc:
        print(f"Error: cannot list {tracker_dir!r}: {exc}", file=sys.stderr)
        return None

    alias_matches: list[str] = []
    for name in entries:
        effective_alias = _effective_alias_for_dir(tracker_dir, name)
        if effective_alias and effective_alias == target:
            alias_matches.append(name)
    return alias_matches


def _resolve_short_id(
    ticket_id: str, tracker_dir: str, *, quiet: bool, dir_names: list[str] | None
) -> str | None:
    """Resolve an 8-hex two-quad short id to its unique ticket dir, or ``None``.

    An exact directory of that name wins first; otherwise the id is a 9-char
    prefix of a full canonical dir name. With ``dir_names`` (a prebuilt sorted
    listing) the prefix run is found by :func:`bisect`; without it the tracker
    root is listed once. An ambiguous (>1) or empty match is ``None`` — the
    ambiguity is reported unless ``quiet``.
    """
    short_hit = _existing_ticket_dir_name(tracker_dir, ticket_id)
    if short_hit is not None:
        return short_hit
    if dir_names is not None:
        matches = _dir_prefix_matches(dir_names, ticket_id)
    else:
        try:
            matches = [
                n
                for n in os.listdir(tracker_dir)
                if not n.startswith(".")
                and n[:9] == ticket_id
                and os.path.isdir(os.path.join(tracker_dir, n))
            ]
        except OSError:
            return None
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and not quiet:
        print(
            f"Error: Ambiguous 8-hex ID '{ticket_id}' matches: {' '.join(sorted(matches))}",
            file=sys.stderr,
        )
    return None


def _resolve_prefix(
    ticket_id: str, tracker_dir: str, *, quiet: bool, dir_names: list[str] | None
) -> str | None:
    """Resolve a >=4-char (compatibility) prefix to its unique ticket dir, or ``None``.

    With ``dir_names`` (a prebuilt sorted listing) the prefix run is found by
    :func:`bisect`; without it the tracker root is listed once. An ambiguous (>1)
    or empty match is ``None`` — the ambiguity is reported unless ``quiet``.
    """
    if len(ticket_id) < 4:
        return None
    if dir_names is not None:
        matches = _dir_prefix_matches(dir_names, ticket_id)
    else:
        try:
            matches = [
                n
                for n in os.listdir(tracker_dir)
                if not n.startswith(".")
                and n.startswith(ticket_id)
                and os.path.isdir(os.path.join(tracker_dir, n))
            ]
        except OSError:
            return None
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1 and not quiet:
        print(
            f"Error: Ambiguous prefix '{ticket_id}' matches multiple tickets: "
            f"{' '.join(sorted(matches))}",
            file=sys.stderr,
        )
    return None


def resolve_ticket_id(
    ticket_id: str,
    tracker_dir: str,
    *,
    quiet: bool = False,
    alias_index: dict[str, list[str]] | None = None,
    dir_names: list[str] | None = None,
) -> str | None:
    """Return the canonical ticket directory name for ``ticket_id``, or None.

    Ambiguous matches and tracker-listing failures are surfaced via stderr to
    match the bash side's diagnostics; the function still returns None so callers
    can pick their own error vs graceful path. ``quiet=True`` suppresses those
    stderr diagnostics (the return value is unchanged) — for scanners resolving
    candidates the user never supplied, e.g. the close precheck's walk over
    historical commit references, where an unrelated ambiguity is noise.

    ``alias_index`` / ``dir_names`` — when a caller resolves MANY ids against the
    same store, it can prebuild both with :func:`build_resolver_scan_index` and
    pass them here; the alias branch then does an in-memory lookup and the
    short-id / generic-prefix branches a :func:`bisect` over the sorted dir names,
    instead of a fresh full-store listing per candidate — keeping the whole walk
    to one store pass. The result is identical to per-call scanning (same match
    set, same ambiguity handling).
    """
    # Fast path: if the input already names an existing ticket directory, use it
    # directly. `_existing_ticket_dir_name` guards the input as a safe path
    # segment and containment-checks the candidate against tracker_dir before any
    # filesystem access, returning a separator-free basename — so a traversing or
    # absolute id can never resolve to (or escape via) the tracker. This also
    # avoids a per-call alias-resolver pass for already-resolved inputs (e.g. a
    # dependency-graph BFS over known directory names), and is unambiguous — a
    # directory matching the input name exactly is that ticket.
    fast = _existing_ticket_dir_name(tracker_dir, ticket_id)
    if fast is not None:
        return fast

    if _FULL_ID_RE.match(ticket_id):
        return _existing_ticket_dir_name(tracker_dir, ticket_id)

    if _SHORT_ID_RE.match(ticket_id):
        return _resolve_short_id(ticket_id, tracker_dir, quiet=quiet, dir_names=dir_names)

    # Jira issue key (e.g. REB-310) → bound local ticket via the binding store.
    # Checked before the alias scan; the shapes are disjoint, so this only matches
    # genuine Jira keys.
    if _JIRA_KEY_RE.match(ticket_id):
        bound = _resolve_via_binding_store(ticket_id, tracker_dir)
        if bound is not None:
            return bound

    alias_matches = (
        alias_index.get(ticket_id, [])
        if alias_index is not None
        else _scan_alias(ticket_id, tracker_dir)
    )
    if alias_matches is not None:
        if len(alias_matches) == 1:
            return alias_matches[0]
        if len(alias_matches) > 1:
            if not quiet:
                print(
                    f"Error: Ambiguous alias '{ticket_id}' matches multiple tickets: "
                    f"{' '.join(sorted(alias_matches))}",
                    file=sys.stderr,
                )
            return None

    return _resolve_prefix(ticket_id, tracker_dir, quiet=quiet, dir_names=dir_names)


def resolve_ticket_dir_name(ticket_id: str, tracker_dir: str) -> str:
    """Resolve ``ticket_id`` to its canonical ticket-directory NAME — a single,
    separator-free segment contained in ``tracker_dir`` — or raise
    ``FileNotFoundError`` when it does not resolve to a real ticket within the
    tracker (including a hostile ``../x`` / absolute id, which
    :func:`resolve_ticket_id` rejects).

    Read sites that build a filesystem path from a ticket id use this in place of
    the unsafe ``resolve_ticket_id(id) or id`` idiom, which resurrected the raw
    (possibly traversing) id whenever resolution failed. Those callers are
    best-effort readers whose existing ``FileNotFoundError`` handlers already map
    "no such ticket" to "no records", so raising here degrades them gracefully.
    """
    name = resolve_ticket_id(ticket_id, tracker_dir)
    if name is None:
        raise FileNotFoundError(f"unresolved ticket id: {ticket_id!r}")
    # `name` is already a safe segment from resolve_ticket_id; basename makes the
    # separator-free guarantee explicit (and is a recognized path-injection
    # barrier for the join the caller builds from the return value).
    return os.path.basename(name)
