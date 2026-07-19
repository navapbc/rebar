"""One-time backfill of retroactive ``caused_by`` links over CLOSED bugs (ticket 2f8e).

For every closed bug we reuse the ticket-555e conservative single-culprit blame resolver
(:func:`rebar.metrics.blame.derive_caused_by`) to find the change that most likely
introduced the bug, then draw a ``caused_by`` link from the bug to that culprit ticket —
the links the bug-close hook would have drawn had the resolver existed at close time.

The blame resolver is best-effort and conservative: it only yields a culprit when a
strict majority (> 50%) of the pre-fix blamed lines belong to ONE commit that resolves
to a ticket, returning ``None`` on any ambiguity. We do NOT reimplement blame here.

Because a closed bug is a closed link *source*, :func:`graph._links.add_dependency` would
reject the edge; we write via the lower-level ``_write_link_event`` (bypassing the
closed-source guard), exactly as ticket 555e does at close time. The pass is idempotent:
a bug that already carries an active ``caused_by`` edge to its culprit is skipped.

``--dry-run`` is the DEFAULT — the script previews proposals and writes nothing unless
``--write`` is passed.
"""

from __future__ import annotations

import argparse

import rebar
from rebar import config
from rebar.graph import _links
from rebar.metrics.blame import derive_caused_by


def propose_caused_by(repo_root: str) -> list[dict]:
    """Proposed ``caused_by`` edges for closed bugs with a resolved single culprit.

    Iterates CLOSED bugs, runs the 555e blame resolver for each, and returns
    ``[{"bug_id": ..., "culprit_id": ...}, ...]`` for the bugs that resolve to exactly
    one culprit ticket. Idempotency (skipping an edge that already exists) is enforced at
    write time in :func:`backfill`, so a re-run never writes a duplicate ``caused_by`` event.
    """
    tracker_dir = str(config.tracker_dir(repo_root))
    proposals: list[dict] = []
    for bug in rebar.list_tickets(status="closed", ticket_type="bug", repo_root=repo_root):
        bug_id = bug["ticket_id"]
        culprit_id = derive_caused_by(bug_id, repo_root, tracker_dir)
        if culprit_id is None:
            continue
        proposals.append({"bug_id": bug_id, "culprit_id": culprit_id})
    return proposals


def backfill(repo_root: str, write: bool = False) -> int:
    """Draw the proposed ``caused_by`` links; return the count of NEW links written.

    In the default dry-run (``write=False``) nothing is written and ``0`` is returned.
    With ``write=True`` each proposed edge is written via the closed-source-bypassing
    ``_write_link_event`` and the number of newly written links is returned.
    """
    proposals = propose_caused_by(repo_root)
    if not write:
        return 0
    tracker_dir = str(config.tracker_dir(repo_root))
    written = 0
    for proposal in proposals:
        bug_id, culprit_id = proposal["bug_id"], proposal["culprit_id"]
        # Idempotency: never write a duplicate edge (see ticket 555e's own close hook,
        # which may already have drawn this link).
        if _links._is_active_link(bug_id, culprit_id, "caused_by", tracker_dir):
            continue
        _links._write_link_event(bug_id, culprit_id, "caused_by", tracker_dir)
        written += 1
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="repository root (default: .)")
    parser.add_argument(
        "--write",
        action="store_true",
        help="actually draw the links (default: dry-run preview only)",
    )
    args = parser.parse_args()

    if args.write:
        count = backfill(args.repo_root, write=True)
        print(f"wrote {count} caused_by link(s)")  # noqa: T201 — CLI output
    else:
        proposals = propose_caused_by(args.repo_root)
        print(  # noqa: T201 — CLI output
            f"[dry-run] {len(proposals)} caused_by link(s) would be written:"
        )
        for proposal in proposals:
            print(  # noqa: T201 — CLI output
                f"  {proposal['bug_id']} -> {proposal['culprit_id']}"
            )


if __name__ == "__main__":
    main()
