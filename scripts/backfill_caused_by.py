"""Backfill ``caused_by`` links on closed bugs (ticket 2f8e).

Reuse ``derive_caused_by``: it yields one culprit only when a strict majority of
pre-fix blamed lines belong to one commit resolving to a ticket. Write the link
through ``_write_link_event`` because the source bug is closed; skip an existing
active edge to the same culprit. Default ``--dry-run`` previews only; ``--write``
persists links."""

from __future__ import annotations

import argparse

import rebar
from rebar import config
from rebar.graph import _links
from rebar.metrics.blame import derive_caused_by


def propose_caused_by(repo_root: str) -> list[dict]:
    """Return closed-bug/single-culprit pairs from the blame resolver. ``backfill`` skips
    existing edges at write time."""
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
    """Write proposed closed-source links and count new edges; default dry-run writes none and
    returns 0."""
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
        # These proposals come from blame auto-derivation, so mark the edge "derived"
        # (ticket 6536-367c) — the escape-rate lens must not read a backfilled guess
        # as a supplied attribution.
        _links._write_link_event(bug_id, culprit_id, "caused_by", tracker_dir, provenance="derived")
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
        print(f"wrote {count} caused_by link(s)")
    else:
        proposals = propose_caused_by(args.repo_root)
        print(f"[dry-run] {len(proposals)} caused_by link(s) would be written:")
        for proposal in proposals:
            print(f"  {proposal['bug_id']} -> {proposal['culprit_id']}")


if __name__ == "__main__":
    main()
