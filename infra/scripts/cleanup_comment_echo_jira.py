#!/usr/bin/env python3
"""Coordinate the incident-specific Jira side of the comment-echo reclaim."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import stat
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

import comment_echo_reclaim_manifest as manifest_tools

CANARY_ORDER = ("REB-2605", "REB-1931", "REB-1567")
JournalOutcomeState: TypeAlias = Literal["completed", "retryable"]
JournalProgressState: TypeAlias = Literal["pending", "retryable", "unresolved"]
CleanupAuthority: TypeAlias = Mapping[tuple[str, str], Mapping[str, str]]


class CleanupError(RuntimeError):
    """The frozen cleanup evidence is incomplete or has drifted."""


class JiraTransport(Protocol):
    """The narrow Jira REST boundary used by the cleanup state machine."""

    def fetch_issue(self, jira_key: str) -> list[dict[str, Any]]: ...

    def delete_comment(self, jira_key: str, comment_id: str) -> int: ...


class JiraCloudTransport:
    """One-attempt Jira Cloud REST v3 transport for the quiet-window tool."""

    def __init__(
        self,
        jira_url: str,
        user: str,
        api_token: str,
        *,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
        timeout: float = 30,
    ) -> None:
        self._jira_url = jira_url.rstrip("/")
        self._auth = base64.b64encode(f"{user}:{api_token}".encode()).decode()
        self._urlopen = urlopen
        self._timeout = timeout

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str],
        *,
        urlopen: Callable[..., Any] = urllib.request.urlopen,
    ) -> JiraCloudTransport:
        required = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")
        missing = [name for name in required if not environ.get(name, "").strip()]
        if missing:
            raise CleanupError(
                "live Jira cleanup requires environment variable(s): " + ", ".join(missing)
            )
        jira_url = environ["JIRA_URL"].strip()
        parsed = urllib.parse.urlsplit(jira_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise CleanupError("JIRA_URL must be an absolute https URL")
        return cls(
            jira_url,
            environ["JIRA_USER"].strip(),
            environ["JIRA_API_TOKEN"],
            urlopen=urlopen,
        )

    def fetch_issue(self, jira_key: str) -> list[dict[str, Any]]:
        pages: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        start_at = 0
        total: int | None = None
        while total is None or start_at < total:
            query = urllib.parse.urlencode({"maxResults": 100, "startAt": start_at})
            key = urllib.parse.quote(jira_key, safe="")
            request = urllib.request.Request(
                f"{self._jira_url}/rest/api/3/issue/{key}/comment?{query}",
                method="GET",
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Basic {self._auth}",
                },
            )
            with self._urlopen(request, timeout=self._timeout) as response:
                page = json.loads(response.read())
            if not isinstance(page, dict):
                raise CleanupError(f"Jira comments page is malformed for {jira_key}")
            comments = page.get("comments")
            page_start = page.get("startAt")
            maximum = page.get("maxResults")
            page_total = page.get("total")
            if (
                not isinstance(comments, list)
                or page_start != start_at
                or not isinstance(maximum, int)
                or maximum <= 0
                or not isinstance(page_total, int)
                or page_total < 0
                or (total is not None and page_total != total)
                or len(comments) > maximum
                or start_at + len(comments) > page_total
                or (not comments and start_at < page_total)
            ):
                raise CleanupError(f"Jira comment pagination is incomplete for {jira_key}")
            for comment in comments:
                comment_id = comment.get("id") if isinstance(comment, dict) else None
                if not isinstance(comment_id, str) or not comment_id or comment_id in seen_ids:
                    raise CleanupError(
                        f"Jira comment pagination repeats or omits an identity for {jira_key}"
                    )
                seen_ids.add(comment_id)
            pages.append(page)
            total = page_total
            start_at += len(comments)
        return pages

    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        key = urllib.parse.quote(jira_key, safe="")
        identity = urllib.parse.quote(comment_id, safe="")
        request = urllib.request.Request(
            f"{self._jira_url}/rest/api/3/issue/{key}/comment/{identity}",
            method="DELETE",
            headers={
                "Accept": "application/json",
                "Authorization": f"Basic {self._auth}",
            },
        )
        with self._urlopen(request, timeout=self._timeout) as response:
            status = response.status
            response.read()
        if not isinstance(status, int):
            raise CleanupError("Jira DELETE response omitted its HTTP status")
        return status


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    try:
        raw = _read_regular(path, "manifest")
        manifest = json.loads(raw)
    except ValueError as exc:
        raise CleanupError(f"manifest is unreadable or invalid JSON: {path}") from exc
    if not isinstance(manifest, dict) or raw != manifest_tools.canonical_json(manifest) + b"\n":
        raise CleanupError("manifest is not the exact canonical JSON artifact")
    digest = manifest.get("manifest_digest")
    unsigned = dict(manifest)
    unsigned.pop("manifest_digest", None)
    calculated = hashlib.sha256(manifest_tools.canonical_json(unsigned)).hexdigest()
    if not isinstance(digest, str) or not hmac.compare_digest(digest, calculated):
        raise CleanupError("manifest digest does not bind its canonical content")
    return manifest, digest


def _read_regular(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CleanupError(f"{label} must be a regular file: {path}") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise CleanupError(f"{label} must be a regular file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            return stream.read()
    finally:
        os.close(descriptor)


def _action_plan(
    manifest: Mapping[str, Any],
    expected_actions: int,
    required_authority: CleanupAuthority,
) -> list[dict[str, Any]]:
    groups = manifest.get("groups")
    if not isinstance(groups, list) or len(groups) != expected_actions:
        raise CleanupError(f"manifest must contain exactly {expected_actions} cleanup actions")
    if len(required_authority) != expected_actions:
        raise CleanupError("configured incident authority does not contain the exact action count")
    actions: list[dict[str, Any]] = []
    identities: set[tuple[str, str]] = set()
    group_identities: set[tuple[str, str]] = set()
    for group in groups:
        cleanup = group.get("jira_cleanup") if isinstance(group, dict) else None
        survivor = cleanup.get("survivor") if isinstance(cleanup, dict) else None
        delete = cleanup.get("delete") if isinstance(cleanup, dict) else None
        authority = cleanup.get("authority") if isinstance(cleanup, dict) else None
        ticket_id = group.get("ticket_id") if isinstance(group, dict) else None
        body_sha256 = group.get("body_sha256") if isinstance(group, dict) else None
        jira_key = group.get("jira_key") if isinstance(group, dict) else None
        if (
            not isinstance(ticket_id, str)
            or not isinstance(body_sha256, str)
            or not isinstance(jira_key, str)
            or not isinstance(survivor, dict)
            or not isinstance(delete, dict)
            or not isinstance(survivor.get("id"), str)
            or not isinstance(delete.get("id"), str)
            or not isinstance(survivor.get("normalized_body"), str)
            or not isinstance(delete.get("normalized_body"), str)
            or survivor["id"] == delete["id"]
        ):
            raise CleanupError("manifest contains a malformed Jira cleanup action")
        group_identity = (ticket_id, body_sha256)
        expected_authority = required_authority.get(group_identity)
        if (
            expected_authority is None
            or not isinstance(authority, dict)
            or authority != expected_authority
            or authority.get("jira_key") != jira_key
            or authority.get("survivor_id") != survivor["id"]
            or authority.get("delete_id") != delete["id"]
            or authority.get("author_account_id") != survivor.get("author_account_id")
            or authority.get("author_account_id") != delete.get("author_account_id")
            or hashlib.sha256(survivor["normalized_body"].encode()).hexdigest() != body_sha256
            or hashlib.sha256(delete["normalized_body"].encode()).hexdigest() != body_sha256
            or group_identity in group_identities
        ):
            raise CleanupError("manifest differs from the fixed incident cleanup authority")
        group_identities.add(group_identity)
        identity = (jira_key, delete["id"])
        if identity in identities:
            raise CleanupError("manifest contains a duplicate Jira cleanup identity")
        identities.add(identity)
        actions.append(
            {
                "ticket_id": group["ticket_id"],
                "body_sha256": group["body_sha256"],
                "jira_key": jira_key,
                "survivor": survivor,
                "delete": delete,
                "authority": authority,
            }
        )
    if group_identities != set(required_authority):
        raise CleanupError("manifest omits part of the fixed incident cleanup authority")
    order = {key: index for index, key in enumerate(CANARY_ORDER)}
    return sorted(
        actions,
        key=lambda action: (
            order.get(action["jira_key"], len(order)),
            action["jira_key"],
            action["body_sha256"],
        ),
    )


def _assert_private_artifact_dir(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except OSError as exc:
        raise CleanupError(f"artifact path must be a real directory: {path}") from exc
    if not stat.S_ISDIR(mode):
        raise CleanupError(f"artifact path must be a real directory: {path}")
    if stat.S_IMODE(mode) & 0o077:
        raise CleanupError(f"artifact directory must not be group/world accessible: {path}")


def _mkdir_private_tree(path: Path) -> None:
    missing: list[Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        cursor = cursor.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass
        _assert_private_artifact_dir(directory)


def _artifact_dir(repo: Path, requested: Path) -> Path:
    if requested.absolute().is_symlink():
        raise CleanupError(f"artifact path must be a real directory: {requested.absolute()}")
    resolved = requested.resolve()
    try:
        resolved.relative_to(repo.resolve())
    except ValueError:
        pass
    else:
        raise CleanupError(f"artifact directory must be outside the source repository: {resolved}")
    if not resolved.exists():
        _mkdir_private_tree(resolved)
    _assert_private_artifact_dir(resolved)
    return resolved


def _write_new(path: Path, value: object) -> None:
    _assert_private_artifact_dir(path.parent)
    raw = manifest_tools.canonical_json(value) + b"\n"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError as exc:
        raise CleanupError(f"refusing to overwrite cleanup artifact: {path}") from exc
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _verify_existing(path: Path, value: object) -> None:
    _assert_private_artifact_dir(path.parent)
    expected = manifest_tools.canonical_json(value) + b"\n"
    actual = _read_regular(path, "cleanup artifact")
    if not hmac.compare_digest(actual, expected):
        raise CleanupError(f"cleanup artifact differs from the inspected dry run: {path}")


def _read_canonical_object(path: Path) -> dict[str, Any]:
    _assert_private_artifact_dir(path.parent)
    try:
        raw = _read_regular(path, "cleanup artifact")
        value = json.loads(raw)
    except ValueError as exc:
        raise CleanupError(f"cleanup artifact is unreadable: {path}") from exc
    if not isinstance(value, dict) or raw != manifest_tools.canonical_json(value) + b"\n":
        raise CleanupError(f"cleanup artifact is not canonical JSON: {path}")
    return value


def _read_journal(path: Path) -> list[dict[str, Any]]:
    _assert_private_artifact_dir(path.parent)
    try:
        path.lstat()
    except FileNotFoundError:
        return []
    try:
        lines = _read_regular(path, "cleanup journal").splitlines(keepends=True)
        records = [json.loads(line) for line in lines]
    except ValueError as exc:
        raise CleanupError(f"cleanup journal is unreadable: {path}") from exc
    if any(
        not isinstance(record, dict) or line != manifest_tools.canonical_json(record) + b"\n"
        for line, record in zip(lines, records, strict=True)
    ):
        raise CleanupError(f"cleanup journal is not canonical JSONL: {path}")
    return records


def _append_record(path: Path, value: object) -> None:
    _assert_private_artifact_dir(path.parent)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_APPEND
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise CleanupError(f"cleanup journal is not appendable: {path}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise CleanupError(f"cleanup journal must be a regular file: {path}")
    with os.fdopen(descriptor, "ab") as stream:
        stream.write(manifest_tools.canonical_json(value) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _capture(transport: JiraTransport, actions: list[dict[str, Any]]) -> dict[str, Any]:
    keys = list(dict.fromkeys(action["jira_key"] for action in actions))
    return {
        "issues": [{"key": key, "pages": transport.fetch_issue(key)} for key in keys],
        "schema_version": 1,
        "source": "jira-cloud-rest-v3",
    }


def _validate_observation(observation: Mapping[str, Any], actions: list[dict[str, Any]]) -> None:
    by_key = manifest_tools.inventory_by_key(observation)
    for action in actions:
        by_id = {comment["id"]: comment for comment in by_key[action["jira_key"]]}
        if by_id.get(action["survivor"]["id"]) != action["survivor"]:
            raise CleanupError("live Jira survivor differs from the frozen manifest")
        if by_id.get(action["delete"]["id"]) != action["delete"]:
            raise CleanupError("live Jira deletion target differs from the frozen manifest")


def _target_present(observation: Mapping[str, Any], action: Mapping[str, Any]) -> bool:
    comments = manifest_tools.inventory_by_key(observation)[action["jira_key"]]
    return any(comment["id"] == action["delete"]["id"] for comment in comments)


def _validate_authorized_delta(
    before: Mapping[str, Any],
    current: Mapping[str, Any],
    actions: list[dict[str, Any]],
    completed_count: int,
) -> None:
    expected = manifest_tools.inventory_by_key(before)
    deleted = {(action["jira_key"], action["delete"]["id"]) for action in actions[:completed_count]}
    expected = {
        jira_key: [comment for comment in comments if (jira_key, comment["id"]) not in deleted]
        for jira_key, comments in expected.items()
    }
    observed = manifest_tools.inventory_by_key(current)
    if manifest_tools.canonical_json(observed) != manifest_tools.canonical_json(expected):
        raise CleanupError("live Jira comments drifted outside authorized deletions")


def _intent_record(action: Mapping[str, Any], action_index: int, digest: str) -> dict[str, Any]:
    return {
        "action_index": action_index,
        "comment_id": action["delete"]["id"],
        "jira_key": action["jira_key"],
        "manifest_digest": digest,
        "record_type": "delete_intent",
        "schema_version": 1,
    }


def _outcome_state(record: Mapping[str, Any], intent: Mapping[str, Any]) -> JournalOutcomeState:
    expected = {**intent, "record_type": "delete_outcome"}
    allowed = {*expected, "delete_result", "error_type", "observed_state", "status_code"}
    if (
        any(record.get(key) != value for key, value in expected.items())
        or not set(record) <= allowed
    ):
        raise CleanupError("cleanup journal outcome does not match its intent")
    result = record.get("delete_result")
    observed = record.get("observed_state")
    if record.get("status_code") == 204 and result is None:
        return "completed"
    if result in {"ambiguous", "recovered"} and observed == "target_absent":
        return "completed"
    if result in {"ambiguous", "recovered"} and observed == "target_present":
        return "retryable"
    if result == "postcondition_failed":
        raise CleanupError("previous Jira cleanup postcondition failed")
    raise CleanupError("cleanup journal contains an unsupported outcome")


def _journal_progress(
    records: list[dict[str, Any]], actions: list[dict[str, Any]], digest: str
) -> tuple[int, JournalProgressState]:
    cursor = 0
    action_index = 0
    while cursor < len(records):
        if action_index >= len(actions):
            raise CleanupError("cleanup journal continues past the action plan")
        intent = _intent_record(actions[action_index], action_index, digest)
        if records[cursor] != intent:
            raise CleanupError("cleanup journal intent does not match the action plan")
        cursor += 1
        if cursor == len(records):
            return action_index, "unresolved"
        state = _outcome_state(records[cursor], intent)
        cursor += 1
        if state == "completed":
            action_index += 1
        elif cursor == len(records):
            return action_index, "retryable"
    return action_index, "pending"


def run(
    args: argparse.Namespace,
    *,
    transport: JiraTransport,
    expected_actions: int = 6,
    required_authority: CleanupAuthority = manifest_tools.INCIDENT_CLEANUP_AUTHORITY,
) -> int:
    manifest, digest = _load_manifest(args.manifest)
    if args.execute and not hmac.compare_digest(args.confirm_manifest_digest or "", digest):
        raise CleanupError("execution requires the exact manifest digest confirmation")
    actions = _action_plan(manifest, expected_actions, required_authority)
    artifacts = _artifact_dir(args.repo, args.artifact_dir)
    observation = _capture(transport, actions)
    _assert_private_artifact_dir(artifacts)
    plan = {
        "actions": actions,
        "canary_order": list(CANARY_ORDER),
        "manifest_digest": digest,
        "schema_version": 1,
    }
    if not args.execute:
        _validate_observation(observation, actions)
        _write_new(artifacts / "jira-before.json", observation)
        _write_new(artifacts / "jira-cleanup-plan.json", plan)
        return 0

    _verify_existing(artifacts / "jira-cleanup-plan.json", plan)
    before = _read_canonical_object(artifacts / "jira-before.json")
    _validate_observation(before, actions)
    journal = artifacts / "jira-cleanup-journal.jsonl"
    records = _read_journal(journal)
    completed_count, progress = _journal_progress(records, actions, digest)
    if progress == "unresolved":
        action = actions[completed_count]
        target_present = _target_present(observation, action)
        recovered_count = completed_count if target_present else completed_count + 1
        _validate_authorized_delta(before, observation, actions, recovered_count)
        intent = _intent_record(action, completed_count, digest)
        _append_record(
            journal,
            {
                **intent,
                "delete_result": "recovered",
                "observed_state": ("target_present" if target_present else "target_absent"),
                "record_type": "delete_outcome",
            },
        )
        completed_count = recovered_count
    else:
        _validate_authorized_delta(before, observation, actions, completed_count)

    after_path = artifacts / "jira-after.json"
    if completed_count == len(actions):
        if after_path.exists():
            _verify_existing(after_path, observation)
        else:
            _write_new(after_path, observation)
        return 0
    if not records:
        _verify_existing(artifacts / "jira-before.json", observation)

    final_observation = observation
    for action_index in range(completed_count, len(actions)):
        action = actions[action_index]
        record = _intent_record(action, action_index, digest)
        _append_record(journal, record)
        _assert_private_artifact_dir(artifacts)
        try:
            status = transport.delete_comment(action["jira_key"], action["delete"]["id"])
        except Exception as exc:
            final_observation = _capture(transport, actions)
            target_present = _target_present(final_observation, action)
            observed_count = action_index if target_present else action_index + 1
            try:
                _validate_authorized_delta(before, final_observation, actions, observed_count)
            except CleanupError:
                _append_record(
                    journal,
                    {
                        **record,
                        "delete_result": "postcondition_failed",
                        "error_type": type(exc).__name__,
                        "observed_state": "unexpected_drift",
                        "record_type": "delete_outcome",
                    },
                )
                raise
            _append_record(
                journal,
                {
                    **record,
                    "delete_result": "ambiguous",
                    "error_type": type(exc).__name__,
                    "observed_state": ("target_present" if target_present else "target_absent"),
                    "record_type": "delete_outcome",
                },
            )
            if target_present:
                raise CleanupError(
                    "Jira DELETE had an ambiguous response and its target remains"
                ) from exc
            continue
        if status != 204:
            final_observation = _capture(transport, actions)
            target_present = _target_present(final_observation, action)
            observed_count = action_index if target_present else action_index + 1
            try:
                _validate_authorized_delta(before, final_observation, actions, observed_count)
            except CleanupError:
                _append_record(
                    journal,
                    {
                        **record,
                        "delete_result": "postcondition_failed",
                        "observed_state": "unexpected_drift",
                        "record_type": "delete_outcome",
                        "status_code": status,
                    },
                )
                raise
            _append_record(
                journal,
                {
                    **record,
                    "delete_result": "ambiguous",
                    "observed_state": ("target_present" if target_present else "target_absent"),
                    "record_type": "delete_outcome",
                    "status_code": status,
                },
            )
            if target_present:
                raise CleanupError(f"Jira DELETE returned {status} and its target remains")
            continue
        final_observation = _capture(transport, actions)
        try:
            _validate_authorized_delta(before, final_observation, actions, action_index + 1)
        except CleanupError:
            _append_record(
                journal,
                {
                    **record,
                    "delete_result": "postcondition_failed",
                    "observed_state": "unexpected_drift",
                    "record_type": "delete_outcome",
                    "status_code": status,
                },
            )
            raise
        _append_record(
            journal,
            {**record, "record_type": "delete_outcome", "status_code": status},
        )
    _write_new(after_path, final_observation)
    return 0


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute the incident-specific, manifest-bound Jira comment cleanup."
    )
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-manifest-digest")
    return parser.parse_args(argv)


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    required_authority: CleanupAuthority = manifest_tools.INCIDENT_CLEANUP_AUTHORITY,
) -> int:
    args = _parse_args(argv)
    try:
        transport = JiraCloudTransport.from_environment(
            os.environ if environ is None else environ,
            urlopen=urlopen,
        )
        return run(args, transport=transport, required_authority=required_authority)
    except (CleanupError, manifest_tools.ReclaimError) as exc:
        sys.stderr.write(f"cleanup refused: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
