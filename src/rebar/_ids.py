"""Shared ticket-ID resolution primitives (stdlib-only leaf).

The single resolution seam Python CLIs and the library use, so every surface
accepts the same ID forms: full 16-hex, 8-hex short, alias, Jira issue key
(e.g. ``REB-310``), and unique prefix >= 4 chars.

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

import json
import os
import re
import sys

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
    bindings_path = os.path.join(tracker_dir, ".bridge_state", "bindings.json")
    try:
        with open(bindings_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    reverse = data.get("reverse")
    if not isinstance(reverse, dict):
        return None
    local_id = reverse.get(target) or reverse.get(target.upper())
    if not isinstance(local_id, str) or not local_id:
        return None
    if os.path.isdir(os.path.join(tracker_dir, local_id)):
        return local_id
    return None


def _scan_alias(target: str, tracker_dir: str) -> list[str] | None:
    """Scan tickets for an alias matching ``target`` (in-process alias resolution).

    Returns the list of matching ticket-dir names, or ``None`` on a hard failure
    listing the tracker (a hard failure must not masquerade as "no match").
    Per-ticket I/O errors skip that ticket. Each ticket's effective alias is its
    stored ``data.alias`` (CREATE event, or the latest non-PRECONDITIONS SNAPSHOT
    ``compiled_state`` for compacted tickets) or a backfilled ``compute_alias`` —
    keeping stored-at-create and backfilled-at-resolve aliases in lock-step.
    """
    try:
        entries = sorted(os.listdir(tracker_dir))
    except OSError as exc:
        print(f"Error: cannot list {tracker_dir!r}: {exc}", file=sys.stderr)
        return None

    alias_matches: list[str] = []
    for name in entries:
        if name.startswith("."):
            continue
        ticket_dir = os.path.join(tracker_dir, name)
        if not os.path.isdir(ticket_dir):
            continue
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
            continue
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
                    snap_state = (json.load(f).get("data", {}) or {}).get(
                        "compiled_state", {}
                    ) or {}
                stored_alias = snap_state.get("alias") or ""
            except (OSError, json.JSONDecodeError):
                pass
        effective_alias = stored_alias or compute_alias(name) or ""
        if effective_alias and effective_alias == target:
            alias_matches.append(name)
    return alias_matches


def resolve_ticket_id(ticket_id: str, tracker_dir: str) -> str | None:
    """Return the canonical ticket directory name for ``ticket_id``, or None.

    Ambiguous matches and tracker-listing failures are surfaced via stderr to
    match the bash side's diagnostics; the function still returns None so callers
    can pick their own error vs graceful path.
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
        short_hit = _existing_ticket_dir_name(tracker_dir, ticket_id)
        if short_hit is not None:
            return short_hit
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
        if len(matches) > 1:
            print(
                f"Error: Ambiguous 8-hex ID '{ticket_id}' matches: {' '.join(sorted(matches))}",
                file=sys.stderr,
            )
        return None

    # Jira issue key (e.g. REB-310) → bound local ticket via the binding store.
    # Checked before the alias scan; the shapes are disjoint, so this only matches
    # genuine Jira keys.
    if _JIRA_KEY_RE.match(ticket_id):
        bound = _resolve_via_binding_store(ticket_id, tracker_dir)
        if bound is not None:
            return bound

    alias_matches = _scan_alias(ticket_id, tracker_dir)
    if alias_matches is not None:
        if len(alias_matches) == 1:
            return alias_matches[0]
        if len(alias_matches) > 1:
            print(
                f"Error: Ambiguous alias '{ticket_id}' matches multiple tickets: "
                f"{' '.join(sorted(alias_matches))}",
                file=sys.stderr,
            )
            return None

    if len(ticket_id) >= 4:
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
        if len(matches) > 1:
            print(
                f"Error: Ambiguous prefix '{ticket_id}' matches multiple tickets: "
                f"{' '.join(sorted(matches))}",
                file=sys.stderr,
            )

    return None


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
