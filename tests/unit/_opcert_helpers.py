"""Shared fixtures for op-cert tests under Option B (story 4214).

Option B anchors op-cert key era-validity at the certificate's STORAGE ANCHOR — a TICKETS-BRANCH
commit — and expresses key era boundaries as TICKETS-BRANCH log positions
(``added_at_log_position`` / ``revoked_at_log_position``). So these tests need a real rebar store
whose tickets-branch commits form a resolvable position chain, rather than a bare code repo. This
module builds that store and returns the chain, plus the ssh keypair helper.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    """(private_key_path, 'ssh-ed25519 AAAA…' public line) for a fresh Ed25519 key."""
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def store_with_chain(tmp_path, monkeypatch, n: int) -> tuple[Path, str, list[tuple[str, str]]]:
    """A real rebar store seeded with ``n`` tickets so the tickets branch has a resolvable chain.

    Returns ``(repo, tracker, positions)`` where ``positions`` is a list of
    ``(log_position, tickets_branch_commit)`` sorted OLDEST-first. Each entry's commit is an
    ancestor of every later entry's commit, so callers can pick an early position as a key's
    ``added_at_log_position`` and a later commit as the storage anchor S.
    """
    import rebar
    from rebar._commands._seam import tracker_dir
    from rebar.attest import authorship

    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    for i in range(n):
        rebar.create_ticket("task", f"chain-{i}", repo_root=str(repo))

    tracker = str(tracker_dir(str(repo)))
    commit_map = authorship.build_introducing_commit_map(repo_root=str(repo))
    positions: list[tuple[str, str]] = []
    for d in sorted(os.listdir(tracker)):
        dp = os.path.join(tracker, d)
        if d.startswith(".") or not os.path.isdir(dp):
            continue
        for fn in sorted(os.listdir(dp)):
            if not fn.endswith(".json") or fn.startswith("."):
                continue
            commit = commit_map.get(f"{d}/{fn}")
            if commit:
                positions.append((fn[:-5].rsplit("-", 1)[0], commit))
    positions.sort()
    return repo, tracker, positions
