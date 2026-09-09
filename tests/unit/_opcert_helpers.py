"""Build op-cert test stores with tickets-branch key-era history.

Option B validates a key era at the certificate's storage anchor. These helpers
return a store, an ordered log-position chain, and an SSH key pair.
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
    """Return ``(repo, tracker, positions)`` after creating ``n`` ticket commits.

    Each ordered ``(log_position, commit)`` entry precedes later commits, allowing
    callers to choose a key-era start and a later storage anchor.
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
