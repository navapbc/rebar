from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

TESTS = Path(__file__).parent
SCRIPTS = Path(__file__).parents[2] / "infra" / "scripts"
sys.path.insert(0, str(TESTS))
sys.path.insert(0, str(SCRIPTS))
import cleanup_comment_echo_jira as cleanup  # noqa: E402
import test_comment_echo_reclaim_manifest as manifest_cases  # noqa: E402


class FakeJira:
    def __init__(self, inventory: dict[str, object]) -> None:
        self._pages = {
            issue["key"]: issue["pages"]
            for issue in inventory["issues"]  # type: ignore[index]
        }
        self.fetch_calls: list[str] = []
        self.delete_calls: list[tuple[str, str]] = []

    def fetch_issue(self, jira_key: str) -> list[dict[str, object]]:
        self.fetch_calls.append(jira_key)
        return copy.deepcopy(self._pages[jira_key])  # type: ignore[return-value]

    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        self.delete_calls.append((jira_key, comment_id))
        pages = self._pages[jira_key]
        for page in pages:  # type: ignore[assignment]
            comments = page["comments"]
            page["comments"] = [comment for comment in comments if comment["id"] != comment_id]
        total = sum(len(page["comments"]) for page in pages)  # type: ignore[arg-type]
        for page in pages:  # type: ignore[assignment]
            page["total"] = total
        return 204

    def ids(self, jira_key: str) -> set[str]:
        return {
            comment["id"]
            for page in self._pages[jira_key]  # type: ignore[assignment]
            for comment in page["comments"]
        }


class JournalCheckingJira(FakeJira):
    def __init__(self, inventory: dict[str, object], journal: Path) -> None:
        super().__init__(inventory)
        self._journal = journal
        self.records_at_delete: list[str] = []

    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        self.records_at_delete = [
            json.loads(line)["record_type"] for line in self._journal.read_text().splitlines()
        ]
        return super().delete_comment(jira_key, comment_id)


class AmbiguousAfterDeleteJira(FakeJira):
    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        super().delete_comment(jira_key, comment_id)
        raise TimeoutError("response lost after Jira accepted DELETE")


class AmbiguousBeforeDeleteJira(FakeJira):
    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        self.delete_calls.append((jira_key, comment_id))
        raise TimeoutError("response lost before Jira accepted DELETE")


class UnexpectedStatusAfterDeleteJira(FakeJira):
    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        super().delete_comment(jira_key, comment_id)
        return 500


class SimulatedCrash(BaseException):
    pass


class CrashAfterDeleteJira(FakeJira):
    def __init__(self, inventory: dict[str, object]) -> None:
        super().__init__(inventory)
        self._crash_once = True

    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        status = super().delete_comment(jira_key, comment_id)
        if self._crash_once:
            self._crash_once = False
            raise SimulatedCrash
        return status


class CrashBeforeDeleteJira(FakeJira):
    def __init__(self, inventory: dict[str, object]) -> None:
        super().__init__(inventory)
        self._crash_once = True

    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        if self._crash_once:
            self._crash_once = False
            self.delete_calls.append((jira_key, comment_id))
            raise SimulatedCrash
        return super().delete_comment(jira_key, comment_id)


class DriftingAfterFirstDeleteJira(FakeJira):
    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        status = super().delete_comment(jira_key, comment_id)
        if len(self.delete_calls) == 1:
            first_page = self._pages[jira_key][0]  # type: ignore[index]
            first_page["comments"][0]["body"] = manifest_cases._adf("unexpected edit")
        return status


class UnexpectedStatusWithDriftJira(DriftingAfterFirstDeleteJira):
    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        super().delete_comment(jira_key, comment_id)
        return 500


class PermissionWideningJira(FakeJira):
    def __init__(self, inventory: dict[str, object], artifacts: Path) -> None:
        super().__init__(inventory)
        self._artifacts = artifacts

    def fetch_issue(self, jira_key: str) -> list[dict[str, object]]:
        pages = super().fetch_issue(jira_key)
        self._artifacts.chmod(0o755)
        return pages


class CrashOnSecondDeleteJira(FakeJira):
    def __init__(self, inventory: dict[str, object]) -> None:
        super().__init__(inventory)
        self._crash_once = True

    def delete_comment(self, jira_key: str, comment_id: str) -> int:
        status = super().delete_comment(jira_key, comment_id)
        if self._crash_once and len(self.delete_calls) == 2:
            self._crash_once = False
            raise SimulatedCrash
        return status


class FakeHttpResponse:
    def __init__(self, payload: object, status: int = 200) -> None:
        self._payload = json.dumps(payload).encode()
        self.status = status

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        pass

    def read(self) -> bytes:
        return self._payload


class PaginatedUrlopen:
    def __init__(self, pages: dict[int, dict[str, object]]) -> None:
        self._pages = pages
        self.calls: list[urllib.request.Request] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> FakeHttpResponse:
        del timeout
        self.calls.append(request)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        return FakeHttpResponse(self._pages[int(query["startAt"][0])])


def _six_action_case(
    fixture: manifest_cases.Fixture,
) -> tuple[dict[str, object], dict[str, object]]:
    manifest = manifest_cases._build_manifest(fixture)
    template = manifest["groups"][0]
    pairs = [
        ("REB-1567", "963510", "997143"),
        ("REB-1931", "963788", "997255"),
        ("REB-1567", "963511", "997144"),
        ("REB-2605", "964272", "997293"),
        ("REB-1931", "963789", "997256"),
        ("REB-1567", "963512", "997145"),
    ]
    groups: list[dict[str, object]] = []
    comments_by_key: dict[str, list[dict[str, object]]] = {}
    for index, (jira_key, survivor_id, delete_id) in enumerate(pairs):
        body = f"incident echo body {delete_id}"
        author = {"accountId": "712020:6471376f-4e5e-4ed2-8c05-330827bc387e"}
        survivor_raw = {"author": author, "body": manifest_cases._adf(body), "id": survivor_id}
        delete_raw = {"author": author, "body": manifest_cases._adf(body), "id": delete_id}
        survivor = copy.deepcopy(survivor_raw)
        survivor.update(
            {
                "author_account_id": author["accountId"],
                "normalized_body": cleanup.manifest_tools.normalize_rich_text(survivor_raw["body"]),
            }
        )
        delete = copy.deepcopy(delete_raw)
        delete.update(
            {
                "author_account_id": author["accountId"],
                "normalized_body": cleanup.manifest_tools.normalize_rich_text(delete_raw["body"]),
            }
        )
        group = copy.deepcopy(template)
        group.update(
            {
                "body_sha256": hashlib.sha256(body.encode()).hexdigest(),
                "jira_cleanup": {
                    "authority": {
                        "author_account_id": author["accountId"],
                        "delete_id": delete_id,
                        "import_commit": "a" * 40,
                        "import_run_id": "synthetic-import-run",
                        "jira_key": jira_key,
                        "post_run_id": "synthetic-post-run",
                        "survivor_id": survivor_id,
                    },
                    "delete": delete,
                    "survivor": survivor,
                },
                "jira_key": jira_key,
                "ticket_id": f"0000-0000-0000-{index:04d}",
            }
        )
        groups.append(group)
        comments_by_key.setdefault(jira_key, []).extend([survivor_raw, delete_raw])
    manifest["groups"] = groups
    manifest.pop("manifest_digest", None)
    manifest["manifest_digest"] = hashlib.sha256(
        cleanup.manifest_tools._canonical(manifest)
    ).hexdigest()
    fixture.output.write_bytes(cleanup.manifest_tools._canonical(manifest) + b"\n")
    issues = []
    for jira_key, comments in comments_by_key.items():
        unrelated = {
            "author": {"accountId": "human-account"},
            "body": manifest_cases._adf(f"unrelated {jira_key}"),
            "id": f"unrelated-{jira_key}",
        }
        all_comments = [unrelated, *comments]
        issues.append(
            {
                "key": jira_key,
                "pages": [
                    {
                        "comments": all_comments,
                        "maxResults": 100,
                        "startAt": 0,
                        "total": len(all_comments),
                    }
                ],
            }
        )
    return manifest, {
        "issues": issues,
        "schema_version": 1,
        "source": "jira-cloud-rest-v3",
    }


def _manifest_authority(manifest: dict[str, object]) -> cleanup.CleanupAuthority:
    groups = manifest["groups"]
    assert isinstance(groups, list)
    return {
        (group["ticket_id"], group["body_sha256"]): group["jira_cleanup"]["authority"]
        for group in groups
    }


def _run(
    args: argparse.Namespace,
    *,
    transport: cleanup.JiraTransport,
    expected_actions: int = 6,
) -> int:
    manifest = json.loads(args.manifest.read_bytes())
    return cleanup.run(
        args,
        transport=transport,
        expected_actions=expected_actions,
        required_authority=_manifest_authority(manifest),
    )


def test_default_run_is_get_only_and_writes_an_inspectable_plan(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = FakeJira(json.loads(fixture.inventory.read_bytes()))

    result = _run(
        argparse.Namespace(
            repo=fixture.repo,
            manifest=fixture.output,
            artifact_dir=artifacts,
            execute=False,
            confirm_manifest_digest=None,
        ),
        transport=transport,
        expected_actions=1,
    )

    assert (
        result,
        transport.delete_calls,
        (artifacts / "jira-before.json").is_file(),
        (artifacts / "jira-cleanup-plan.json").is_file(),
    ) == (0, [], True, True)


def test_default_run_creates_every_missing_artifact_ancestor_private(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "operator" / "incident" / "jira-artifacts"
    transport = FakeJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    previous_umask = os.umask(0o022)
    try:
        result = _run(args, transport=transport, expected_actions=1)
    finally:
        os.umask(previous_umask)

    assert (
        result,
        [
            path.stat().st_mode & 0o777
            for path in (artifacts.parent.parent, artifacts.parent, artifacts)
        ],
    ) == (0, [0o700, 0o700, 0o700])


def test_execute_without_the_exact_manifest_digest_refuses_before_delete(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = FakeJira(json.loads(fixture.inventory.read_bytes()))
    dry_run = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(dry_run, transport=transport, expected_actions=1)

    try:
        _run(
            argparse.Namespace(**{**vars(dry_run), "execute": True}),
            transport=transport,
            expected_actions=1,
        )
    except cleanup.CleanupError as exc:
        outcome = str(exc)
    else:
        outcome = "no refusal"

    assert ("manifest digest" in outcome, transport.delete_calls) == (True, [])


def test_self_redigested_manifest_cannot_replace_fixed_incident_authority(
    tmp_path: Path,
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    manifest["groups"][0]["jira_cleanup"]["authority"]["delete_id"] = "forged-target"
    manifest.pop("manifest_digest")
    manifest["manifest_digest"] = hashlib.sha256(
        cleanup.manifest_tools._canonical(manifest)
    ).hexdigest()
    fixture.output.write_bytes(cleanup.manifest_tools._canonical(manifest) + b"\n")
    artifacts = tmp_path / "jira-artifacts"
    transport = FakeJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=True,
        confirm_manifest_digest=manifest["manifest_digest"],
    )

    with pytest.raises(cleanup.CleanupError, match="fixed incident cleanup authority"):
        cleanup.run(
            args,
            transport=transport,
            expected_actions=1,
            required_authority=manifest_cases.CLEANUP_AUTHORITY,
        )

    assert (transport.fetch_calls, transport.delete_calls, artifacts.exists()) == ([], [], False)


def test_digest_confirmed_execution_deletes_only_the_manifest_identity(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = FakeJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]

    result = _run(args, transport=transport, expected_actions=1)

    assert (
        result,
        transport.delete_calls,
        {"200", "300"} & transport.ids(manifest_cases.JIRA_KEY),
        (artifacts / "jira-after.json").is_file(),
    ) == (0, [(manifest_cases.JIRA_KEY, "300")], {"200"}, True)


def test_execution_journals_intent_before_delete_and_outcome_afterward(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    journal = artifacts / "jira-cleanup-journal.jsonl"
    inventory = json.loads(fixture.inventory.read_bytes())
    transport = JournalCheckingJira(inventory, journal)
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]

    _run(args, transport=transport, expected_actions=1)
    records = [json.loads(line) for line in journal.read_text().splitlines()]

    assert (
        transport.records_at_delete,
        [(record["record_type"], record["jira_key"], record["comment_id"]) for record in records],
    ) == (
        ["delete_intent"],
        [
            ("delete_intent", manifest_cases.JIRA_KEY, "300"),
            ("delete_outcome", manifest_cases.JIRA_KEY, "300"),
        ],
    )


def test_ambiguous_delete_is_reread_once_without_retrying(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = AmbiguousAfterDeleteJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]

    result = _run(args, transport=transport, expected_actions=1)
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        result,
        transport.delete_calls,
        transport.fetch_calls,
        {"200", "300"} & transport.ids(manifest_cases.JIRA_KEY),
        (
            records[-1]["record_type"],
            records[-1]["delete_result"],
            records[-1]["observed_state"],
        ),
    ) == (
        0,
        [(manifest_cases.JIRA_KEY, "300")],
        [manifest_cases.JIRA_KEY] * 3,
        {"200"},
        ("delete_outcome", "ambiguous", "target_absent"),
    )


def test_ambiguous_retained_target_is_journaled_and_stops_without_retry(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = AmbiguousBeforeDeleteJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]

    try:
        _run(args, transport=transport, expected_actions=1)
    except cleanup.CleanupError as exc:
        refusal = str(exc)
    else:
        refusal = "no refusal"
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        refusal,
        transport.delete_calls,
        transport.fetch_calls,
        [
            (
                record["record_type"],
                record.get("delete_result"),
                record.get("observed_state"),
            )
            for record in records
        ],
        (artifacts / "jira-after.json").exists(),
    ) == (
        "Jira DELETE had an ambiguous response and its target remains",
        [(manifest_cases.JIRA_KEY, "300")],
        [manifest_cases.JIRA_KEY] * 3,
        [
            ("delete_intent", None, None),
            ("delete_outcome", "ambiguous", "target_present"),
        ],
        False,
    )


def test_restart_retries_a_persisted_retained_target_once(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    inventory = json.loads(fixture.inventory.read_bytes())
    artifacts = tmp_path / "jira-artifacts"
    first_transport = AmbiguousBeforeDeleteJira(copy.deepcopy(inventory))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=first_transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]
    with pytest.raises(cleanup.CleanupError, match="ambiguous response and its target remains"):
        _run(args, transport=first_transport, expected_actions=1)

    resumed_transport = FakeJira(copy.deepcopy(inventory))
    result = _run(args, transport=resumed_transport, expected_actions=1)
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        result,
        first_transport.delete_calls,
        resumed_transport.delete_calls,
        [
            (
                record["record_type"],
                record.get("delete_result"),
                record.get("observed_state"),
                record.get("status_code"),
            )
            for record in records
        ],
        "300" in resumed_transport.ids(manifest_cases.JIRA_KEY),
        "200" in resumed_transport.ids(manifest_cases.JIRA_KEY),
        (artifacts / "jira-after.json").is_file(),
    ) == (
        0,
        [(manifest_cases.JIRA_KEY, "300")],
        [(manifest_cases.JIRA_KEY, "300")],
        [
            ("delete_intent", None, None, None),
            ("delete_outcome", "ambiguous", "target_present", None),
            ("delete_intent", None, None, None),
            ("delete_outcome", None, None, 204),
        ],
        False,
        True,
        True,
    )


def test_non_204_delete_is_reread_once_without_retrying(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = UnexpectedStatusAfterDeleteJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]

    result = _run(args, transport=transport, expected_actions=1)
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        result,
        transport.delete_calls,
        transport.fetch_calls,
        (
            records[-1]["delete_result"],
            records[-1]["observed_state"],
            records[-1]["status_code"],
        ),
    ) == (
        0,
        [(manifest_cases.JIRA_KEY, "300")],
        [manifest_cases.JIRA_KEY] * 3,
        ("ambiguous", "target_absent", 500),
    )


def test_non_204_delete_with_unrelated_drift_seals_postcondition_failure(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = UnexpectedStatusWithDriftJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]

    with pytest.raises(cleanup.CleanupError, match="drifted outside authorized deletions"):
        _run(args, transport=transport, expected_actions=1)
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        transport.delete_calls,
        [
            (
                record["record_type"],
                record.get("delete_result"),
                record.get("observed_state"),
                record.get("status_code"),
            )
            for record in records
        ],
        (artifacts / "jira-after.json").exists(),
    ) == (
        [(manifest_cases.JIRA_KEY, "300")],
        [
            ("delete_intent", None, None, None),
            ("delete_outcome", "postcondition_failed", "unexpected_drift", 500),
        ],
        False,
    )


def test_restart_recovers_intent_after_delete_without_a_second_delete(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = CrashAfterDeleteJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]
    try:
        _run(args, transport=transport, expected_actions=1)
    except SimulatedCrash:
        pass

    result = _run(args, transport=transport, expected_actions=1)
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        result,
        transport.delete_calls,
        transport.fetch_calls,
        [
            (
                record["record_type"],
                record.get("delete_result"),
                record.get("observed_state"),
            )
            for record in records
        ],
        (artifacts / "jira-after.json").is_file(),
    ) == (
        0,
        [(manifest_cases.JIRA_KEY, "300")],
        [manifest_cases.JIRA_KEY] * 3,
        [
            ("delete_intent", None, None),
            ("delete_outcome", "recovered", "target_absent"),
        ],
        True,
    )


def test_restart_recovers_intent_before_delete_and_retries_once(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = CrashBeforeDeleteJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]
    with pytest.raises(SimulatedCrash):
        _run(args, transport=transport, expected_actions=1)

    result = _run(args, transport=transport, expected_actions=1)
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        result,
        transport.delete_calls,
        transport.fetch_calls,
        [
            (
                record["record_type"],
                record.get("delete_result"),
                record.get("observed_state"),
                record.get("status_code"),
            )
            for record in records
        ],
        (artifacts / "jira-after.json").is_file(),
    ) == (
        0,
        [
            (manifest_cases.JIRA_KEY, "300"),
            (manifest_cases.JIRA_KEY, "300"),
        ],
        [manifest_cases.JIRA_KEY] * 4,
        [
            ("delete_intent", None, None, None),
            ("delete_outcome", "recovered", "target_present", None),
            ("delete_intent", None, None, None),
            ("delete_outcome", None, None, 204),
        ],
        True,
    )


def test_completed_execution_is_idempotent_and_verifies_sealed_after_state(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = FakeJira(json.loads(fixture.inventory.read_bytes()))
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport, expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]
    _run(args, transport=transport, expected_actions=1)
    journal = artifacts / "jira-cleanup-journal.jsonl"
    after = artifacts / "jira-after.json"
    sealed = (journal.read_bytes(), after.read_bytes())

    result = _run(args, transport=transport, expected_actions=1)

    assert (
        result,
        transport.delete_calls,
        transport.fetch_calls,
        (journal.read_bytes(), after.read_bytes()),
    ) == (
        0,
        [(manifest_cases.JIRA_KEY, "300")],
        [manifest_cases.JIRA_KEY] * 4,
        sealed,
    )


def test_execution_refuses_a_symlinked_before_artifact_before_delete(tmp_path: Path) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    inventory = json.loads(fixture.inventory.read_bytes())
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=FakeJira(inventory), expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]
    with pytest.raises(cleanup.CleanupError, match="target remains"):
        _run(
            args,
            transport=AmbiguousBeforeDeleteJira(inventory),
            expected_actions=1,
        )
    before = artifacts / "jira-before.json"
    preserved = tmp_path / "substituted-before.json"
    preserved.write_bytes(before.read_bytes())
    before.unlink()
    before.symlink_to(preserved)
    resumed = FakeJira(inventory)

    with pytest.raises(cleanup.CleanupError, match="must be a regular file"):
        _run(args, transport=resumed, expected_actions=1)

    assert resumed.delete_calls == []


def test_execution_rechecks_artifact_directory_permissions_after_jira_get(
    tmp_path: Path,
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest = manifest_cases._build_manifest(fixture)
    artifacts = tmp_path / "jira-artifacts"
    inventory = json.loads(fixture.inventory.read_bytes())
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=FakeJira(inventory), expected_actions=1)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]
    transport = PermissionWideningJira(inventory, artifacts)

    with pytest.raises(cleanup.CleanupError, match="group/world accessible"):
        _run(args, transport=transport, expected_actions=1)

    assert transport.delete_calls == []


def test_canary_stops_before_second_delete_when_any_non_target_comment_drifts(
    tmp_path: Path,
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest, inventory = _six_action_case(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = DriftingAfterFirstDeleteJira(inventory)
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]

    try:
        _run(args, transport=transport)
    except cleanup.CleanupError as exc:
        refusal = str(exc)
    else:
        refusal = "no refusal"
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        refusal,
        transport.delete_calls,
        [(record["record_type"], record.get("delete_result")) for record in records],
        (artifacts / "jira-after.json").exists(),
    ) == (
        "live Jira comments drifted outside authorized deletions",
        [("REB-2605", "997293")],
        [
            ("delete_intent", None),
            ("delete_outcome", "postcondition_failed"),
        ],
        False,
    )

    sealed_journal = (artifacts / "jira-cleanup-journal.jsonl").read_bytes()
    with pytest.raises(cleanup.CleanupError, match="previous Jira cleanup postcondition failed"):
        _run(args, transport=transport)
    assert (
        transport.delete_calls,
        (artifacts / "jira-cleanup-journal.jsonl").read_bytes(),
    ) == ([("REB-2605", "997293")], sealed_journal)


def test_restart_resumes_six_action_canary_without_redeleting_completed_targets(
    tmp_path: Path,
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest, inventory = _six_action_case(fixture)
    artifacts = tmp_path / "jira-artifacts"
    transport = CrashOnSecondDeleteJira(inventory)
    args = argparse.Namespace(
        repo=fixture.repo,
        manifest=fixture.output,
        artifact_dir=artifacts,
        execute=False,
        confirm_manifest_digest=None,
    )
    _run(args, transport=transport)
    args.execute = True
    args.confirm_manifest_digest = manifest["manifest_digest"]
    try:
        _run(args, transport=transport)
    except SimulatedCrash:
        pass

    result = _run(args, transport=transport)
    records = [
        json.loads(line)
        for line in (artifacts / "jira-cleanup-journal.jsonl").read_text().splitlines()
    ]

    assert (
        result,
        [jira_key for jira_key, _comment_id in transport.delete_calls],
        {comment_id for _jira_key, comment_id in transport.delete_calls},
        len(transport.delete_calls),
        [record.get("delete_result") for record in records].count("recovered"),
        len(records),
        (artifacts / "jira-after.json").is_file(),
    ) == (
        0,
        ["REB-2605", "REB-1931", "REB-1931", "REB-1567", "REB-1567", "REB-1567"],
        {"997143", "997144", "997145", "997255", "997256", "997293"},
        6,
        1,
        12,
        True,
    )


def test_cloud_transport_fetches_every_comment_page_without_gaps() -> None:
    pages = {
        0: {"comments": [{"id": "1"}, {"id": "2"}], "maxResults": 2, "startAt": 0, "total": 3},
        2: {"comments": [{"id": "3"}], "maxResults": 2, "startAt": 2, "total": 3},
    }
    urlopen = PaginatedUrlopen(pages)
    transport = cleanup.JiraCloudTransport(
        "https://jira.example.test",
        "operator@example.test",
        "secret-token",
        urlopen=urlopen,
    )

    fetched = transport.fetch_issue("REB-1")

    assert (
        fetched,
        [
            urllib.parse.parse_qs(urllib.parse.urlsplit(call.full_url).query)["startAt"][0]
            for call in urlopen.calls
        ],
        [call.get_method() for call in urlopen.calls],
        all(call.get_header("Authorization", "").startswith("Basic ") for call in urlopen.calls),
    ) == (list(pages.values()), ["0", "2"], ["GET", "GET"], True)


def test_cloud_transport_rejects_a_repeated_identity_across_comment_pages() -> None:
    pages = {
        0: {
            "comments": [{"id": "1"}, {"id": "2"}],
            "maxResults": 2,
            "startAt": 0,
            "total": 3,
        },
        2: {
            "comments": [{"id": "2"}],
            "maxResults": 2,
            "startAt": 2,
            "total": 3,
        },
    }
    urlopen = PaginatedUrlopen(pages)
    transport = cleanup.JiraCloudTransport(
        "https://jira.example.test",
        "operator@example.test",
        "secret-token",
        urlopen=urlopen,
    )

    try:
        transport.fetch_issue("REB-1")
    except cleanup.CleanupError as exc:
        refusal = str(exc)
    else:
        refusal = "no refusal"

    assert (refusal, len(urlopen.calls)) == (
        "Jira comment pagination repeats or omits an identity for REB-1",
        2,
    )


def test_cloud_transport_issues_exactly_one_delete_attempt() -> None:
    calls: list[urllib.request.Request] = []

    def record_delete(request: urllib.request.Request, *, timeout: float) -> FakeHttpResponse:
        del timeout
        calls.append(request)
        return FakeHttpResponse(None, status=204)

    transport = cleanup.JiraCloudTransport(
        "https://jira.example.test",
        "operator@example.test",
        "secret-token",
        urlopen=record_delete,
    )

    status = transport.delete_comment("REB-1", "997293")

    assert (
        status,
        len(calls),
        calls[0].get_method(),
        urllib.parse.urlsplit(calls[0].full_url).path,
        calls[0].get_header("Authorization", "").startswith("Basic "),
    ) == (204, 1, "DELETE", "/rest/api/3/issue/REB-1/comment/997293", True)


def test_cli_refuses_missing_credentials_before_http_or_artifact_write(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[urllib.request.Request] = []

    def forbidden_http(request: urllib.request.Request, *, timeout: float) -> FakeHttpResponse:
        del timeout
        calls.append(request)
        raise AssertionError("HTTP must not run without complete credentials")

    artifacts = tmp_path / "jira-artifacts"
    result = cleanup.main(
        [
            "--repo",
            str(tmp_path / "repo"),
            "--manifest",
            str(tmp_path / "manifest.json"),
            "--artifact-dir",
            str(artifacts),
        ],
        environ={
            "JIRA_URL": "https://jira.example.test",
            "JIRA_USER": "operator@example.test",
        },
        urlopen=forbidden_http,
    )
    captured = capsys.readouterr()

    assert (
        result,
        calls,
        artifacts.exists(),
        "JIRA_API_TOKEN" in captured.err,
        "secret-token" in captured.err,
    ) == (2, [], False, True, False)


def test_cli_refuses_invalid_live_inventory_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    manifest, inventory = _six_action_case(fixture)
    invalid_inventory = copy.deepcopy(inventory)
    first_issue = invalid_inventory["issues"][0]  # type: ignore[index]
    first_issue["pages"][0]["comments"][0]["body"] = "not-adf"
    pages = {
        issue["key"]: issue["pages"]
        for issue in invalid_inventory["issues"]  # type: ignore[index]
    }
    calls: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> FakeHttpResponse:
        del timeout
        calls.append(request)
        jira_key = urllib.parse.unquote(urllib.parse.urlsplit(request.full_url).path).split("/")[-2]
        return FakeHttpResponse(pages[jira_key][0])

    artifacts = tmp_path / "jira-artifacts"
    result = cleanup.main(
        [
            "--repo",
            str(fixture.repo),
            "--manifest",
            str(fixture.output),
            "--artifact-dir",
            str(artifacts),
        ],
        environ={
            "JIRA_API_TOKEN": "secret-token",
            "JIRA_URL": "https://jira.example.test",
            "JIRA_USER": "operator@example.test",
        },
        urlopen=urlopen,
        required_authority=_manifest_authority(manifest),
    )
    captured = capsys.readouterr()

    assert (
        result,
        [call.get_method() for call in calls],
        list(artifacts.iterdir()),
        captured.err.startswith("cleanup refused: Jira inventory comment identity is invalid"),
        "Traceback" in captured.err,
    ) == (2, ["GET", "GET", "GET"], [], True, False)


def test_cli_wires_environment_transport_into_a_six_action_get_only_dry_run(
    tmp_path: Path,
) -> None:
    fixture = manifest_cases._build_fixture(tmp_path / "fixture")
    _manifest, inventory = _six_action_case(fixture)
    pages = {
        issue["key"]: issue["pages"]
        for issue in inventory["issues"]  # type: ignore[index]
    }
    calls: list[urllib.request.Request] = []

    def urlopen(request: urllib.request.Request, *, timeout: float) -> FakeHttpResponse:
        del timeout
        calls.append(request)
        jira_key = urllib.parse.unquote(urllib.parse.urlsplit(request.full_url).path).split("/")[-2]
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
        start_at = int(query["startAt"][0])
        page = next(page for page in pages[jira_key] if page["startAt"] == start_at)
        return FakeHttpResponse(page)

    artifacts = tmp_path / "jira-artifacts"
    result = cleanup.main(
        [
            "--repo",
            str(fixture.repo),
            "--manifest",
            str(fixture.output),
            "--artifact-dir",
            str(artifacts),
        ],
        environ={
            "JIRA_API_TOKEN": "secret-token",
            "JIRA_URL": "https://jira.example.test",
            "JIRA_USER": "operator@example.test",
        },
        urlopen=urlopen,
        required_authority=_manifest_authority(_manifest),
    )
    plan = json.loads((artifacts / "jira-cleanup-plan.json").read_bytes())

    assert (
        result,
        len(plan["actions"]),
        [call.get_method() for call in calls],
        sorted(
            urllib.parse.unquote(urllib.parse.urlsplit(call.full_url).path).split("/")[-2]
            for call in calls
        ),
        (artifacts / "jira-cleanup-journal.jsonl").exists(),
        (artifacts / "jira-after.json").exists(),
    ) == (0, 6, ["GET", "GET", "GET"], ["REB-1567", "REB-1931", "REB-2605"], False, False)
