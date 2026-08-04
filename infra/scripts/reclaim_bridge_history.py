#!/usr/bin/env python3
"""Dry-run removal of historical Jira bridge caches from a tickets branch.

This is intentionally a one-time, local-only tool.  It rewrites an output ref with
stock ``git fast-export``/``git fast-import`` plumbing and never invokes ``git push``.
The final current bridge-state files are re-added to the rewritten head, while their
historical revisions (and the retarget backup) are absent from its ancestry.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import BinaryIO

BRIDGE_STATE = b".bridge_state/"
RETARGET_BACKUP = b".bridge_state.bak-retarget/"
STRIPPED_PREFIXES = (BRIDGE_STATE, RETARGET_BACKUP)
PROTECTED_TAG = "refs/tags/pre-heal-a118-20260710T005736Z"


class ReclaimError(RuntimeError):
    """A dry-run precondition or postcondition was not met."""


def is_stripped_path(path: bytes) -> bool:
    """Whether a fast-export file command addresses reclaimed bridge state."""
    return path.startswith(STRIPPED_PREFIXES)


def _copy_data_block(source: BinaryIO, destination: BinaryIO, header: bytes) -> None:
    """Copy one ``data <n>`` record without inspecting its arbitrary payload bytes."""
    try:
        length = int(header[5:].strip())
    except ValueError as exc:
        raise ReclaimError(f"malformed fast-export data header: {header!r}") from exc
    payload = source.read(length)
    if len(payload) != length:
        raise ReclaimError("truncated fast-export data payload")
    destination.write(header)
    destination.write(payload)


def filter_export_stream(source: BinaryIO, destination: BinaryIO) -> None:
    """Copy a fast-export stream while dropping bridge-state ``M`` and ``D`` commands.

    The stream is binary-framed: command-looking bytes inside a ``data <n>`` payload
    (including a commit message beginning with ``M ``) are copied verbatim, never
    mistaken for file commands.
    """
    while line := source.readline():
        if line.startswith(b"data "):
            _copy_data_block(source, destination, line)
            continue
        if line.startswith(b"M "):
            fields = line.rstrip(b"\n").split(b" ", 3)
            if len(fields) == 4 and is_stripped_path(fields[3]):
                continue
        elif line.startswith(b"D ") and is_stripped_path(line[2:].rstrip(b"\n")):
            continue
        destination.write(line)


def run_git(
    repo: Path,
    args: Sequence[str],
    *,
    input_bytes: bytes | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run Git in ``repo`` and raise an operator-readable error on failure."""
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        input=input_bytes,
        capture_output=True,
        env=env,
    )
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        raise ReclaimError(f"git {' '.join(args)} failed: {detail}")
    return completed


def rev_parse(repo: Path, ref: str) -> str:
    return run_git(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"]).stdout.decode().strip()


def ref_name(repo: Path, ref: str) -> str:
    return run_git(repo, ["rev-parse", "--symbolic-full-name", ref]).stdout.decode().strip()


def ref_count(repo: Path, ref: str, *, merges: bool = False) -> int:
    args = ["rev-list", "--count"]
    if merges:
        args.append("--merges")
    args.append(ref)
    return int(run_git(repo, args).stdout)


def ref_exists(repo: Path, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "-C", str(repo), "show-ref", "--verify", "--quiet", ref],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def protected_remote_refs(repo: Path, source_ref: str) -> bytes | None:
    """Snapshot only the remote refs this offline dry run promises not to move."""
    remotes = run_git(repo, ["remote"]).stdout.splitlines()
    if b"origin" not in remotes:
        return None
    source_name = ref_name(repo, source_ref)
    return run_git(repo, ["ls-remote", "origin", source_name, PROTECTED_TAG]).stdout


def is_partial_clone(repo: Path) -> bool:
    """Whether Git config declares a promisor or partial-clone remote.

    Object presence in a small rehearsal is not proof that a partial clone is safe:
    fast-export may demand a promised object only on the full production history.
    """
    promisor = subprocess.run(
        ["git", "-C", str(repo), "config", "--bool", "--get-regexp", r"^remote\..*\.promisor$"],
        capture_output=True,
    )
    if promisor.returncode == 0:
        if any(line.rsplit(maxsplit=1)[-1] == b"true" for line in promisor.stdout.splitlines()):
            return True
    elif promisor.returncode != 1:
        detail = promisor.stderr.decode(errors="replace").strip()
        raise ReclaimError(f"could not inspect promisor configuration: {detail}")
    partial_filter = subprocess.run(
        ["git", "-C", str(repo), "config", "--get-regexp", r"^remote\..*\.partialclonefilter$"],
        capture_output=True,
    )
    if partial_filter.returncode == 0:
        return True
    if partial_filter.returncode != 1:
        detail = partial_filter.stderr.decode(errors="replace").strip()
        raise ReclaimError(f"could not inspect partial-clone configuration: {detail}")
    return False


def export_and_import(repo: Path, source_ref: str, output_ref: str) -> None:
    """Stream a filtered export directly into fast-import, without an intermediate file."""
    source_name = ref_name(repo, source_ref)
    output_name = output_ref if output_ref.startswith("refs/") else f"refs/heads/{output_ref}"
    exporter = subprocess.Popen(
        [
            "git",
            "-C",
            str(repo),
            "fast-export",
            "--no-data",
            "--signed-tags=strip",
            f"--refspec={source_name}:{output_name}",
            source_name,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    importer = subprocess.Popen(
        ["git", "-C", str(repo), "fast-import", "--quiet"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert exporter.stdout is not None and importer.stdin is not None
    try:
        filter_export_stream(exporter.stdout, importer.stdin)
        importer.stdin.close()
        importer.wait()
        importer_stdout = importer.stdout.read() if importer.stdout is not None else b""
        importer_stderr = importer.stderr.read() if importer.stderr is not None else b""
        exporter_stderr = exporter.stderr.read() if exporter.stderr is not None else b""
    finally:
        exporter.stdout.close()
    if exporter.wait() != 0:
        detail = exporter_stderr.decode(errors="replace").strip()
        raise ReclaimError(f"git fast-export failed: {detail}")
    if importer.returncode:
        detail = (importer_stderr or importer_stdout).decode(errors="replace").strip()
        raise ReclaimError(f"git fast-import failed: {detail}")


_IDENTITY = re.compile(rb"^(.*) <([^<>]*)> ([^ ]+ [^ ]+)$")


def _commit_metadata(repo: Path, commit: str) -> tuple[dict[str, str], list[str], bytes]:
    raw = run_git(repo, ["cat-file", "commit", commit]).stdout
    headers, message = raw.split(b"\n\n", 1)
    author = committer = None
    parents: list[str] = []
    for line in headers.splitlines():
        if line.startswith(b"author "):
            author = line[7:]
        elif line.startswith(b"committer "):
            committer = line[10:]
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode())
    if author is None or committer is None:
        raise ReclaimError(f"commit {commit} has incomplete identity metadata")
    env: dict[str, str] = {}
    for label, value in (("AUTHOR", author), ("COMMITTER", committer)):
        match = _IDENTITY.match(value)
        if match is None:
            raise ReclaimError(f"commit {commit} has malformed {label.lower()} identity")
        name, email, date = (part.decode("utf-8", "surrogateescape") for part in match.groups())
        env[f"GIT_{label}_NAME"] = name
        env[f"GIT_{label}_EMAIL"] = email
        env[f"GIT_{label}_DATE"] = date
    return env, parents, message


def restore_current_bridge_state(repo: Path, source_tip: str, output_ref: str) -> None:
    """Replace the imported head with the same commit metadata and current bridge tree."""
    imported_tip = rev_parse(repo, output_ref)
    entries = run_git(
        repo,
        [
            "ls-tree",
            "-r",
            "-z",
            "--format=%(objectmode) %(objectname)%x09%(path)",
            source_tip,
            "--",
            ".bridge_state/",
        ],
    ).stdout
    if not entries:
        raise ReclaimError("source tip has no current .bridge_state entries to re-add")
    fd, index_path = tempfile.mkstemp(prefix="reclaim-bridge-index-")
    os.close(fd)
    os.unlink(index_path)
    index_env = {**os.environ, "GIT_INDEX_FILE": index_path}
    try:
        run_git(repo, ["read-tree", f"{imported_tip}^{{tree}}"], env=index_env)
        run_git(
            repo,
            ["update-index", "-z", "--index-info"],
            input_bytes=entries,
            env=index_env,
        )
        tree = run_git(repo, ["write-tree"], env=index_env).stdout.decode().strip()
    finally:
        try:
            os.unlink(index_path)
        except FileNotFoundError:
            pass
    env, parents, message = _commit_metadata(repo, imported_tip)
    command = ["git", "-C", str(repo), "commit-tree", tree]
    for parent in parents:
        command.extend(("-p", parent))
    completed = subprocess.run(
        command,
        input=message,
        capture_output=True,
        env={**os.environ, **env},
    )
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        raise ReclaimError(f"git commit-tree failed: {detail}")
    amended_tip = completed.stdout.decode().strip()
    run_git(repo, ["update-ref", output_ref, amended_tip, imported_tip])


def pack_size(repo: Path, ref: str) -> int:
    command = ["git", "-C", str(repo), "pack-objects", "--stdout", "--revs"]
    with tempfile.TemporaryFile() as pack:
        completed = subprocess.run(
            command,
            input=f"{ref}\n".encode(),
            stdout=pack,
            stderr=subprocess.PIPE,
        )
        size = pack.tell()
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        raise ReclaimError(f"git pack-objects failed: {detail}")
    return size


def assert_rewrite(
    repo: Path,
    output_ref: str,
    source_commits: int,
    source_merges: int,
    source_tree: str,
    max_pack_bytes: int,
) -> None:
    rewritten = rev_parse(repo, output_ref)
    if ref_count(repo, rewritten) != source_commits:
        raise ReclaimError("rewritten commit count differs from source")
    if ref_count(repo, rewritten, merges=True) != source_merges:
        raise ReclaimError("rewritten merge count differs from source")
    if run_git(repo, ["rev-parse", f"{rewritten}^{{tree}}"]).stdout.decode().strip() != source_tree:
        raise ReclaimError("rewritten head tree differs from source head tree")
    for prefix in (".bridge_state/", ".bridge_state.bak-retarget/"):
        history = run_git(repo, ["log", "--format=%H", rewritten, "--", prefix]).stdout.splitlines()
        if prefix == ".bridge_state/":
            if history != [rewritten.encode()]:
                raise ReclaimError("bridge-state history is not limited to the rewritten head")
        elif history:
            raise ReclaimError("retarget backup remains in rewritten history")
    if pack_size(repo, rewritten) > max_pack_bytes:
        raise ReclaimError("rewritten pack exceeds --max-pack-bytes")


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--source-ref", default="tickets")
    parser.add_argument("--output-ref", required=True)
    parser.add_argument("--max-pack-bytes", required=True, type=int)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        repo = args.repo.resolve()
        if not (repo / ".git").exists():
            raise ReclaimError(f"not a Git worktree: {repo}")
        if run_git(repo, ["status", "--porcelain"]).stdout:
            raise ReclaimError("scratch worktree is not clean")
        if is_partial_clone(repo):
            raise ReclaimError(
                "partial/promisor clone is unsafe for this rewrite; use a fresh unfiltered clone"
            )
        source_tip = rev_parse(repo, args.source_ref)
        output_name = (
            args.output_ref
            if args.output_ref.startswith("refs/")
            else f"refs/heads/{args.output_ref}"
        )
        if ref_exists(repo, output_name):
            raise ReclaimError(f"output ref already exists: {output_name}")
        if run_git(
            repo,
            ["ls-tree", "-r", "--name-only", source_tip, "--", ".bridge_state.bak-retarget/"],
        ).stdout:
            raise ReclaimError("source tip still has a retarget backup; start from the current tip")
        old_refs = protected_remote_refs(repo, args.source_ref)
        source_commits = ref_count(repo, source_tip)
        source_merges = ref_count(repo, source_tip, merges=True)
        source_tree = run_git(repo, ["rev-parse", f"{source_tip}^{{tree}}"]).stdout.decode().strip()
        export_and_import(repo, args.source_ref, output_name)
        restore_current_bridge_state(repo, source_tip, output_name)
        assert_rewrite(
            repo,
            output_name,
            source_commits,
            source_merges,
            source_tree,
            args.max_pack_bytes,
        )
        if protected_remote_refs(repo, args.source_ref) != old_refs:
            raise ReclaimError("source tickets ref or protected tag changed during dry run")
    except ReclaimError as exc:
        sys.stderr.write(f"DRY RUN FAILED: {exc}\n")
        return 1
    sys.stdout.write(
        "DRY RUN SUCCESS: rewritten history assertions passed; no push was performed\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
