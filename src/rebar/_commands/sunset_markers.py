"""Sunset-ticket markers: the admission grammar, and the close refusal that gives it teeth.

A ``# mechanism-ok:`` marker admits a mechanism past the mechanism-delta ratchet. That
admission used to be permanent: any nonblank prose admitted the key forever, so nothing
could ever establish that the justification had expired — the migration closed, the marker
stayed, and the ratchet never looked again. An admission now has to name a SUNSET TICKET,
and that ticket cannot close while its marker is still in the tree.

Both halves read the grammar from HERE so they cannot drift apart: ``sunset_ticket`` is the
single definition of what a marker's reason must open with, imported by the lint-time
ratchet in ``scripts/_mechanism_delta/markers.py`` and used by the close precheck below. A
grammar that disagreed with the precheck would admit markers no close could ever discharge.
"""

from __future__ import annotations

import re
import subprocess

from rebar._commands._seam import CommandError

MARKER = "mechanism-ok:"

# A sunset ticket id opening a marker's reason: the four-quad canonical form, the two-quad
# short form, or a three-word alias. A bare 4-digit single quad is NOT accepted — AGENTS.md
# deprecates it as a reference form because those quads collide and so resolve to nothing,
# which is exactly the permanent non-reference this grammar exists to reject.
_QUAD = r"[0-9a-f]{4}"
_SUNSET_RE = re.compile(rf"^({_QUAD}(?:-{_QUAD}){{3}}|{_QUAD}-{_QUAD}|[a-z]+(?:-[a-z]+){{2}})\b")

_MARKER_RE = re.compile(r"#\s*mechanism-ok:(.*)")

# Accepted name/reason separators, matched in their whitespace-surrounded form so a
# mechanism name may itself contain spaces or ``::``.
_SEPARATORS = ("—", "--", "-", ":")
_NAME_REASON_RE = re.compile(r"\s+(?:" + "|".join(re.escape(s) for s in _SEPARATORS) + r")\s+")


def sunset_ticket(reason: str) -> str:
    """Return the rebar ticket id a marker reason opens with, or ``""`` when it names none.

    This is a SHAPE check that never opens the store. The lint-time ratchet runs in
    checkouts where the ``tickets`` ref need not be fetched, so resolving the id there would
    add a dependency it does not have. A well-formed id naming no existing ticket passes
    here; the close precheck is what gives the id its meaning.
    """
    match = _SUNSET_RE.match(reason.strip())
    return match.group(1) if match else ""


def marker_reason(line: str) -> str:
    """Return the reason field of a ``# mechanism-ok:`` marker, or ``""`` for a non-marker."""
    match = _MARKER_RE.search(line)
    if match is None:
        return ""
    _kind, _, rest = match.group(1).strip().partition(" ")
    rest = rest.strip()
    split = _NAME_REASON_RE.search(rest)
    if split is not None:
        return rest[split.end() :].strip()
    _name, _, reason = rest.partition(" ")
    reason = reason.strip()
    for sep in _SEPARATORS:
        if reason.startswith(sep):
            return reason[len(sep) :].strip()
    return reason


def _has_repository(code_root: str) -> bool:
    """Return whether a git repository is reachable from ``code_root``.

    A missing directory and a directory outside any repository both answer False, so the
    scan's benign case is decided by git's own structural answer rather than by its prose.
    """
    try:
        probe = subprocess.run(
            ["git", "-C", code_root, "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return probe.returncode == 0


def _marker_lines(code_root: str) -> list[tuple[str, int, str]]:
    """Return ``(path, lineno, text)`` for every marker in a TRACKED file.

    ``git grep`` reads the tracked set in one subprocess rather than walking the worktree,
    so an untracked scratch file carrying a marker cannot block a close.

    A code root with no git repository yields no markers, because there is no tracked set to
    carry one — that is an empty answer, not a failure. Every OTHER failure raises: a guard
    that answers "no markers" when it could not look is a guard that silently stops guarding,
    and this one decides whether a close may proceed.

    The two are told apart by asking git whether a repository is there at all, rather than by
    matching its error text, so a reworded or localized message cannot turn a failed scan into
    a silent pass.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", code_root, "grep", "-n", "--fixed-strings", "--", MARKER],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError(f"cannot scan {code_root} for {MARKER} markers: {exc}") from exc
    # ``git grep`` exits 1 for "no matches", which is not an error here.
    if proc.returncode not in (0, 1):
        if not _has_repository(code_root):
            return []
        stderr = (proc.stderr or "").strip()
        raise RuntimeError(
            f"cannot scan {code_root} for {MARKER} markers: "
            f"git grep exited {proc.returncode}: {stderr or '(no stderr)'}"
        )
    out: list[tuple[str, int, str]] = []
    for raw in proc.stdout.splitlines():
        path, _, rest = raw.partition(":")
        lineno, _, text = rest.partition(":")
        if path and lineno.isdigit():
            out.append((path, int(lineno), text))
    return out


def _names_ticket(marker_id: str, resolved_id: str, tracker: str) -> bool:
    """Whether ``marker_id`` and ``resolved_id`` are the same ticket, both canonicalized.

    The cheap shape comparisons run first so the store is opened only for an alias — without
    that resolution the one alias-citing marker in the tree could never block its own close.
    """
    if marker_id == resolved_id:
        return True
    if resolved_id.startswith(f"{marker_id}-"):
        return True
    if re.fullmatch(rf"{_QUAD}(?:-{_QUAD})*", marker_id):
        return False
    from rebar._ids import resolve_ticket_id

    # ``quiet=True``: these are ids the user never supplied, so an unrelated ambiguity in a
    # marker's citation is noise on the close path, not a diagnostic. Resolution failure
    # returns None rather than raising.
    return resolve_ticket_id(marker_id, tracker, quiet=True) == resolved_id


def open_sunset_sites(resolved_id: str, code_root: str, tracker: str) -> list[tuple[str, int]]:
    """Return ``(path, lineno)`` for every tracked marker naming ``resolved_id`` as its sunset."""
    sites: list[tuple[str, int]] = []
    for path, lineno, text in _marker_lines(code_root):
        marker_id = sunset_ticket(marker_reason(text))
        if marker_id and _names_ticket(marker_id, resolved_id, tracker):
            sites.append((path, lineno))
    return sites


def check_no_open_sunset_marker(
    ticket_id: str, resolved_id: str, code_root: str, tracker: str
) -> None:
    """Refuse the close while a tracked marker still names this ticket as its sunset.

    This is the half that makes the admission a two-way door. Closing the sunset ticket is
    the project's statement that the justification expired, so letting the close through
    while the marker stands would restore exactly the permanent admission the grammar
    retired.
    """
    sites = open_sunset_sites(resolved_id, code_root, tracker)
    if not sites:
        return
    listed = "; ".join(f"{path}:{lineno}" for path, lineno in sites[:5])
    more = "" if len(sites) <= 5 else f" (and {len(sites) - 5} more)"
    raise CommandError(
        f"Error: cannot close {ticket_id}: it is named as the sunset ticket of a "
        f"'# mechanism-ok:' marker still in the tree at {listed}{more}. Closing it would "
        "leave that mechanism admitted with no ticket left to retire it. Remove the marker "
        "(and the mechanism) or repoint it at the ticket that will retire it, then retry "
        '(or override with --force="<reason>").',
        returncode=1,
    )
