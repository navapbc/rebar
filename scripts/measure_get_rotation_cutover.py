#!/usr/bin/env python3
"""Measure packed cost before and after the GET-rotation cutover.

The input is a real tickets-store repository.  The driver selects consecutive
``bindings.json`` revisions, rebuilds them in clean repositories, and compares
the committed A2-1 dual-write shape with the A2-2 sidecar-only projection.  Both
repositories use identical commit metadata and the production JSON formatting.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src" / "rebar" / "_engine"))

from rebar_reconciler.inbound_fields import normalize_baseline_value  # noqa: E402

_BINDINGS = ".bridge_state/bindings.json"
_ROTATION = ".bridge_state/get_rotation.json"
_NORMALIZED_BASELINE_FIELDS = ("description", "priority", "status")


# raw-git-ok: source calls are read-only; write callers use only disposable rebuilt repos
def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env=env,
    ).stdout


def _revisions(repo: Path, count: int, end_ref: str) -> list[str]:
    output = _git(repo, "log", end_ref, "--format=%H", "--", _BINDINGS).decode().splitlines()
    if len(output) < count:
        raise ValueError(f"requested {count} revisions, found {len(output)}")
    return list(reversed(output[:count]))


def _canonical(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()


def _snapshot(repo: Path, revision: str, *, cutover: bool) -> tuple[bytes, bytes]:
    bindings_raw = _git(repo, "show", f"{revision}:{_BINDINGS}")
    bindings = json.loads(bindings_raw)
    try:
        rotation = json.loads(_git(repo, "show", f"{revision}:{_ROTATION}"))
    except subprocess.CalledProcessError:
        rotation = {"version": 1, "last_get_pass": {}}
    entries = bindings.get("bindings")
    stamps = rotation.get("last_get_pass")
    if not isinstance(entries, dict) or not isinstance(stamps, dict):
        raise ValueError(f"{revision} has an invalid rotation-store shape")

    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        entry.pop("baseline_advanced_at", None)
        baseline = entry.get("baseline")
        if isinstance(baseline, dict):
            for field in _NORMALIZED_BASELINE_FIELDS:
                if field in baseline:
                    baseline[field] = normalize_baseline_value(field, baseline[field])
        jira_key = entry.get("jira_key")
        legacy = entry.get("last_get_pass")
        if isinstance(jira_key, str):
            current = stamps.get(jira_key)
            current_value = current if isinstance(current, str) else ""
            legacy_value = legacy if isinstance(legacy, str) else ""
            maximum = max(current_value, legacy_value)
            if maximum:
                stamps[jira_key] = maximum
                if not cutover:
                    entry["last_get_pass"] = maximum
        if cutover:
            entry.pop("last_get_pass", None)
    return _canonical(bindings), _canonical(rotation)


def _pack_bytes(repo: Path) -> int:
    _git(repo, "repack", "-adf", "--window=250", "--depth=50")
    pack_dir = repo / ".git" / "objects" / "pack"
    return sum(path.stat().st_size for path in pack_dir.glob("*.pack"))


def _measure(repo: Path, revisions: list[str], *, cutover: bool) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="rebar-a2-pack-") as temp_name:
        rebuilt = Path(temp_name)
        _git(rebuilt.parent, "init", "-q", "-b", "tickets", str(rebuilt))
        _git(rebuilt, "config", "user.name", "Rebar measurement")
        _git(rebuilt, "config", "user.email", "measure@example.invalid")

        previous: tuple[bytes, bytes] | None = None
        first_pack = 0
        versions = 0
        for corpus_index, revision in enumerate(revisions):
            snapshot = _snapshot(repo, revision, cutover=cutover)
            if snapshot == previous:
                continue
            previous = snapshot
            versions += 1
            bridge = rebuilt / ".bridge_state"
            bridge.mkdir(exist_ok=True)
            (rebuilt / _BINDINGS).write_bytes(snapshot[0])
            (rebuilt / _ROTATION).write_bytes(snapshot[1])
            _git(  # raw-git-ok: stage only in the disposable measurement repository
                rebuilt, "add", _BINDINGS, _ROTATION
            )
            commit_env = dict(os.environ)
            date = f"2000-01-01T00:{corpus_index:02d}:00+00:00"
            commit_env["GIT_AUTHOR_DATE"] = date
            commit_env["GIT_COMMITTER_DATE"] = date
            _git(  # raw-git-ok: commit only in the disposable measurement repository
                rebuilt,
                "commit",
                "-q",
                "--no-verify",
                "-m",
                f"snapshot {corpus_index + 1}",
                env=commit_env,
            )
            if versions == 1:
                first_pack = _pack_bytes(rebuilt)

        if versions < 2:
            raise ValueError("measurement requires at least two distinct versions")
        final_pack = _pack_bytes(rebuilt)
        return {
            "versions": versions,
            "first_pack_bytes": first_pack,
            "final_pack_bytes": final_pack,
            "marginal_bytes_per_changed_version": (final_pack - first_pack) / (versions - 1),
        }


def run(repo: Path, count: int, end_ref: str) -> dict[str, Any]:
    revisions = _revisions(repo, count, end_ref)
    baseline = _measure(repo, revisions, cutover=False)
    cutover = _measure(repo, revisions, cutover=True)
    return {
        "corpus_revisions": count,
        "end_ref": end_ref,
        "oldest_revision": revisions[0],
        "newest_revision": revisions[-1],
        "repack": {"window": 250, "depth": 50},
        "a2_1_dual_write": baseline,
        "a2_2_sidecar_only": cutover,
        "marginal_ratio": cutover["marginal_bytes_per_changed_version"]
        / baseline["marginal_bytes_per_changed_version"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(".tickets-tracker"))
    parser.add_argument("--count", type=int, default=40)
    parser.add_argument("--end-ref", default="HEAD")
    args = parser.parse_args()
    print(  # noqa: T201 - machine-readable measurement output
        json.dumps(run(args.repo.resolve(), args.count, args.end_ref), indent=2, sort_keys=True)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
