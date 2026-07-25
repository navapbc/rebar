"""One-time migration of store tickets from ``## Success Criteria`` to
``## Acceptance Criteria`` checklist items (ticket 0a8c).

Criteria filed under ``## Success Criteria`` are invisible to the check-ac blocking
floor: ``_count_ac_items`` counts ``- [ ]`` items ONLY under ``## Acceptance
Criteria`` (resetting at the next ``##`` heading). This pass moves them under the
umbrella heading as checkboxes so they finally count.

``--dry-run`` is the DEFAULT — the script previews and writes nothing unless
``--write`` is passed. Modifying a non-closed ticket additionally requires
``--include-open`` — closed tickets are migrated freely (the historical-accuracy
cost of touching closed tickets is accepted deliberately), but a still-live
ticket's description is left alone unless explicitly opted in.
"""

from __future__ import annotations

import argparse
import re

import rebar

_HEADING_RE = re.compile(r"(?m)^## .*$")
_SC_HEADING = "## Success Criteria"
_AC_HEADING = "## Acceptance Criteria"
_BULLET_RE = re.compile(r"^(\s*)- (?!\[)(.*)$")


def _sections(description: str) -> list[dict]:
    """Split ``description`` into ``## ``-heading-delimited sections.

    Each section spans from its heading line's start to the next same-level
    (``## ``) heading's start, or end-of-text for the last one. Sub-headings
    (``### ...``) don't match ``## `` and so stay inside their parent section.
    """
    matches = list(_HEADING_RE.finditer(description))
    sections = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(description)
        sections.append(
            {"heading": m.group().strip(), "start": m.start(), "heading_end": m.end(), "end": end}
        )
    return sections


def _checkbox_body(body: str) -> str:
    """Convert ``- `` bullet lines in ``body`` to ``- [ ]`` checklist items.

    Non-bullet lines (blank lines, prose, sub-headings) pass through unchanged.
    """
    out_lines = []
    for line in body.split("\n"):
        m = _BULLET_RE.match(line)
        out_lines.append(f"{m.group(1)}- [ ] {m.group(2)}" if m else line)
    return "\n".join(out_lines)


def rewrite_description(description: str) -> str:
    """Migrate a ticket description's SC block into its AC block.

    Returns the description unchanged when it carries no ``## Success Criteria``.
    """
    sections = _sections(description)
    sc = next((s for s in sections if s["heading"] == _SC_HEADING), None)
    if sc is None:
        return description

    sc_body = description[sc["heading_end"] : sc["end"]]
    sc_checkbox_body = _checkbox_body(sc_body)
    ac = next((s for s in sections if s["heading"] == _AC_HEADING), None)

    if ac is None:
        # No pre-existing AC block: the SC section becomes the AC section in place.
        return (
            description[: sc["start"]] + _AC_HEADING + sc_checkbox_body + description[sc["end"] :]
        )

    # Merge the SC checklist items onto the end of the existing AC block, then
    # drop the SC section entirely.
    items = [ln.strip() for ln in sc_checkbox_body.split("\n") if ln.strip().startswith("- [")]
    ac_body = description[ac["heading_end"] : ac["end"]]
    trailing_ws = ac_body[len(ac_body.rstrip()) :]
    merged_ac_body = ac_body.rstrip() + "\n" + "\n".join(items) + trailing_ws

    return (
        description[: ac["heading_end"]]
        + merged_ac_body
        + description[ac["end"] : sc["start"]]
        + description[sc["end"] :]
    )


def propose_migrations(repo_root: str, *, include_open: bool = False) -> list[dict]:
    """Proposed ``(ticket_id, new_description)`` migrations.

    Scans ticket **descriptions** (via :func:`rebar.list_tickets`, not a repo grep,
    so ``.bridge_state/*.json`` snapshots on the ``tickets`` branch are never in
    scope). Non-closed tickets are skipped unless ``include_open`` is set.
    """
    tickets = rebar.list_tickets(repo_root=repo_root, full=True)
    proposals = []
    for ticket in tickets:
        if ticket["status"] != "closed" and not include_open:
            continue
        description = ticket.get("description") or ""
        new_description = rewrite_description(description)
        if new_description != description:
            proposals.append(
                {
                    "ticket_id": ticket["ticket_id"],
                    "status": ticket["status"],
                    "old_description": description,
                    "new_description": new_description,
                }
            )
    return proposals


def migrate(repo_root: str, *, write: bool = False, include_open: bool = False) -> int:
    """Rewrite proposed descriptions via :func:`rebar.edit_ticket`; return the count.

    In default dry-run (``write=False``) nothing is written and ``0`` is returned.
    With ``write=True`` each proposal is written through ``rebar.edit_ticket`` (a
    normal, auditable ticket event) and the number of tickets migrated is returned.
    """
    proposals = propose_migrations(repo_root, include_open=include_open)
    if not write:
        return 0
    for proposal in proposals:
        rebar.edit_ticket(
            proposal["ticket_id"],
            repo_root=repo_root,
            description=proposal["new_description"],
        )
    return len(proposals)


def _print_preview(proposal: dict) -> None:
    print(  # noqa: T201 — CLI output
        f"  {proposal['ticket_id']} ({proposal['status']}):"
    )
    print("    --- before ---")  # noqa: T201 — CLI output
    for line in proposal["old_description"].splitlines():
        print(f"    {line}")  # noqa: T201 — CLI output
    print("    --- after ---")  # noqa: T201 — CLI output
    for line in proposal["new_description"].splitlines():
        print(f"    {line}")  # noqa: T201 — CLI output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
    parser.add_argument(
        "--write",
        action="store_true",
        help="actually rewrite descriptions (default: dry-run preview only)",
    )
    parser.add_argument(
        "--include-open",
        action="store_true",
        help="also migrate non-closed tickets (default: closed tickets only)",
    )
    args = parser.parse_args()

    if args.write:
        count = migrate(args.repo_root, write=True, include_open=args.include_open)
        print(f"migrated {count} ticket(s)")  # noqa: T201 — CLI output
    else:
        proposals = propose_migrations(args.repo_root, include_open=args.include_open)
        print(  # noqa: T201 — CLI output
            f"[dry-run] {len(proposals)} ticket(s) would be migrated:"
        )
        for proposal in proposals:
            _print_preview(proposal)
        print(f"[dry-run] {len(proposals)} ticket(s) would be migrated")  # noqa: T201 — CLI output


if __name__ == "__main__":
    main()
