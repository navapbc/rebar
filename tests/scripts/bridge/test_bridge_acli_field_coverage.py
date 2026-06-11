"""ACLI client field-extraction + contract-regression tests for the Jira bridge.

This file has TWO test classes:

1. TestAcliClientCreateFieldExtraction / TestAcliClientUpdateFieldExtraction —
   verify which fields actually reach ACLI from a ticket-data dict.

2. TestAcliContractRegression — locks down the EXACT ACLI command shape and
   payload structure for every documented invocation pattern. These tests
   exist because epic 3a03 surfaced three layered ACLI invocation bugs
   (ProjectKey null, priority dict shape, --label vs --labels flag) that
   would have been caught pre-cutover by a command-shape contract test.
   Every confirmed-correct pattern from the 2026-05-24 ACLI audit is pinned
   here; regression (flag rename, payload key drift, missing --yes) will
   fail loudly in CI.

3. TestAcliSanitizers — defends against untrusted user input in ticket
   summaries and labels (whitespace, commas, empty strings, oversize values).

Test: python3 -m pytest tests/scripts/test_bridge_acli_field_coverage.py -v
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest


class TestAcliClientCreateFieldExtraction:
    """Test which fields AcliClient.create_issue() actually sends to the ACLI subprocess.

    The bridge passes the full ticket data dict to acli_client.create_issue(),
    but AcliClient.create_issue() may only extract a subset of those fields
    for the ACLI command. These tests reveal what actually reaches Jira.
    """

    def test_acli_create_sends_summary(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """AcliClient.create_issue() should send the title/summary to ACLI.

        When priority is present, create uses --from-json (so --summary is in
        the JSON payload, not the CLI args). When priority is absent, --summary
        appears as a CLI flag.
        """
        client, captured_cmds, fake_run_acli = acli_capture

        # Without priority: --summary appears as CLI flag
        ticket_data_no_pri = {
            "ticket_type": "bug",
            "title": "Test Summary",
            "assignee": "alice",
            "description": "Test description",
        }

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            try:
                client.create_issue(ticket_data_no_pri)
            except (KeyError, AttributeError):
                pass

        assert len(captured_cmds) >= 1, "At least one ACLI command should be issued"
        create_cmd = captured_cmds[0]
        assert "--summary" in create_cmd, (
            f"ACLI create command should include --summary. Got: {create_cmd}"
        )
        summary_idx = create_cmd.index("--summary")
        assert create_cmd[summary_idx + 1] == "Test Summary"

    def test_acli_create_sends_type(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """AcliClient.create_issue() should send the ticket type to ACLI."""
        client, captured_cmds, fake_run_acli = acli_capture

        ticket_data = {
            "ticket_type": "bug",
            "title": "Test",
        }

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            try:
                client.create_issue(ticket_data)
            except (KeyError, AttributeError):
                pass  # May fail on verify-after-create; we only care about the first call

        assert len(captured_cmds) >= 1
        create_cmd = captured_cmds[0]
        assert "--type" in create_cmd, (
            f"ACLI create command should include --type. Got: {create_cmd}"
        )
        type_idx = create_cmd.index("--type")
        assert create_cmd[type_idx + 1] == "Bug"  # capitalized

    def test_acli_create_sends_description(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """AcliClient.create_issue() should send the description to ACLI."""
        client, captured_cmds, fake_run_acli = acli_capture

        ticket_data = {
            "ticket_type": "bug",
            "title": "Test",
            "description": "Important bug description",
        }

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            try:
                client.create_issue(ticket_data)
            except TypeError:
                pytest.fail(
                    "create_issue() raised TypeError — patch may not be intercepting correctly"
                )

        assert len(captured_cmds) >= 1, "At least one ACLI command should be issued"
        create_cmd = captured_cmds[0]
        assert "--description" in create_cmd, (
            f"ACLI create command should include --description flag. Got: {create_cmd}"
        )

    def test_acli_create_sends_priority_via_from_json(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """AcliClient.create_issue() should send priority via --from-json.

        ACLI does not support --priority on create. Priority is set via
        --from-json with additionalAttributes.priority.name in the JSON payload.
        """
        client, captured_cmds, fake_run_acli = acli_capture

        ticket_data = {
            "ticket_type": "bug",
            "title": "Test",
            "priority": 1,
        }

        dumped_payloads: list[Any] = []

        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            dumped_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                try:
                    client.create_issue(ticket_data)
                except TypeError:
                    pytest.fail(
                        "create_issue() raised TypeError — patch may not be intercepting correctly"
                    )

        assert len(captured_cmds) >= 1, "At least one ACLI command should be issued"
        create_cmd = captured_cmds[0]
        assert "--from-json" in create_cmd, (
            f"When priority is set, ACLI create should use --from-json. Got: {create_cmd}"
        )

        assert dumped_payloads, "json.dump should have been called to write the payload"
        payload = dumped_payloads[0]
        assert "additionalAttributes" in payload, (
            f"Payload should contain 'additionalAttributes'. Got keys: {list(payload.keys())}"
        )
        priority_field = payload["additionalAttributes"].get("priority", {})
        assert "name" in priority_field, (
            f"additionalAttributes.priority should have a 'name' key. Got: {priority_field}"
        )
        assert priority_field["name"] == "High", (
            f"additionalAttributes.priority.name should be 'High' (mapped from int 1). "
            f"Got: {priority_field['name']!r}"
        )

    def test_acli_create_extracts_name_from_dict_shape_priority(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """AcliClient.create_issue() must extract priority.name when priority
        is a Jira REST-shape dict, not stringify the whole dict.

        Bug 5010-1c6a-9387-4b5b: the reconciler differ propagates Jira's
        snapshot priority field (a dict with iconUrl/id/name/self) into the
        create payload. Before this fix, _create_issue_from_json fell through
        the int-branch to str(priority), producing a Python-repr string that
        ACLI rejects with "The priority selected is invalid". After the fix,
        the dict must be unwrapped to its 'name' so the payload contains
        additionalAttributes.priority.name == "High".
        """
        client, captured_cmds, fake_run_acli = acli_capture

        # Jira REST snapshot shape — what fetcher.py:88-93 forwards into the
        # differ-emitted mutation, what applier.py:337-351 then passes through
        # client.create_issue.
        ticket_data = {
            "ticket_type": "bug",
            "title": "Test",
            "priority": {
                "iconUrl": "https://navasage.atlassian.net/images/icons/priorities/high.svg",
                "id": "2",
                "name": "High",
                "self": "https://navasage.atlassian.net/rest/api/3/priority/2",
            },
        }

        dumped_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            dumped_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.create_issue(ticket_data)

        assert dumped_payloads, "json.dump should have been called to write the payload"
        payload = dumped_payloads[0]
        priority_field = payload["additionalAttributes"].get("priority", {})
        assert priority_field == {"name": "High"}, (
            f"additionalAttributes.priority must be exactly {{'name': 'High'}} "
            f"when priority is a Jira-shape dict. Got: {priority_field!r}"
        )

    def test_acli_create_priority_dict_id_only_falls_back_via_reverse_map(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """When the priority dict lacks `name` but has `id`, fall back to the
        reverse-id lookup against _LOCAL_PRIORITY_TO_JIRA. id="2" -> "High"."""
        client, captured_cmds, fake_run_acli = acli_capture
        ticket_data = {
            "ticket_type": "bug",
            "title": "Test id-only",
            "priority": {"id": "2"},  # no 'name' key
        }
        dumped_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            dumped_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.create_issue(ticket_data)

        payload = dumped_payloads[0]
        assert payload["additionalAttributes"]["priority"] == {"name": "High"}, (
            f"id='2' must map to 'High' via reverse-lookup. Got: "
            f"{payload['additionalAttributes']['priority']!r}"
        )

    def test_acli_create_priority_malformed_dict_defaults_to_medium(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """When the priority dict is malformed (no name, no usable id), the
        fallback must default to 'Medium' rather than crashing or sending an
        invalid priority to ACLI."""
        client, captured_cmds, fake_run_acli = acli_capture
        ticket_data = {
            "ticket_type": "bug",
            "title": "Test malformed",
            "priority": {"unexpected_key": "garbage", "id": "not-a-number"},
        }
        dumped_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            dumped_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.create_issue(ticket_data)

        payload = dumped_payloads[0]
        assert payload["additionalAttributes"]["priority"] == {"name": "Medium"}, (
            f"Malformed dict must default to Medium. Got: "
            f"{payload['additionalAttributes']['priority']!r}"
        )

    def test_acli_create_sends_assignee(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """AcliClient.create_issue() should send the assignee to ACLI."""
        client, captured_cmds, fake_run_acli = acli_capture

        ticket_data = {
            "ticket_type": "bug",
            "title": "Test",
            "assignee": "alice",
        }

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            try:
                client.create_issue(ticket_data)
            except TypeError:
                pytest.fail(
                    "create_issue() raised TypeError — patch may not be intercepting correctly"
                )

        assert len(captured_cmds) >= 1, "At least one ACLI command should be issued"
        create_cmd = captured_cmds[0]
        assert "--assignee" in create_cmd, (
            f"ACLI create command should include --assignee flag. Got: {create_cmd}"
        )


class TestAcliClientUpdateFieldExtraction:
    """Test which fields AcliClient.update_issue() sends for non-status field updates."""

    def test_acli_update_routes_priority_to_rest(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """AcliClient.update_issue() routes priority to update_priority (REST PUT).

        ACLI workitem edit does not support --priority. Priority updates are
        now handled via direct REST API (PUT /rest/api/3/issue/{key}).
        """
        client, captured_cmds, fake_run_acli = acli_capture

        with (
            patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli),
            patch.object(acli_mod.acli_cli_ops, "update_priority") as mock_priority,
        ):
            result = client.update_issue("TEST-1", priority="High")

        # No ACLI edit command should be issued for priority-only updates
        assert len(captured_cmds) == 0, (
            f"No ACLI command should be issued for priority-only update. Got: {captured_cmds}"
        )
        assert result == {"key": "TEST-1"}

        # Priority must be routed to update_priority via REST.
        # acli_cmd comes from the fixture's AcliClient (acli_cmd=["echo"])
        mock_priority.assert_called_once_with("TEST-1", "High", acli_cmd=["echo"])

    def test_acli_update_sends_description(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """AcliClient.update_issue() should support sending description updates."""
        client, captured_cmds, fake_run_acli = acli_capture

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            client.update_issue("TEST-1", description="Updated desc")

        assert len(captured_cmds) >= 1
        edit_cmd = captured_cmds[0]
        assert "--description" in edit_cmd, (
            f"ACLI edit command should include --description. Got: {edit_cmd}"
        )

    def test_acli_update_description_uses_adf_format(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """AcliClient.update_issue() description must be sent as ADF JSON, not plain text."""
        import json

        client, captured_cmds, fake_run_acli = acli_capture

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            client.update_issue("TEST-1", description="Test ADF conversion")

        assert len(captured_cmds) >= 1
        edit_cmd = captured_cmds[0]
        desc_idx = edit_cmd.index("--description")
        desc_value = edit_cmd[desc_idx + 1]
        parsed = json.loads(desc_value)
        assert parsed.get("type") == "doc", (
            f"Description should be ADF format with type='doc'. Got: {desc_value[:100]}"
        )
        assert parsed.get("version") == 1, "ADF version should be 1"
        assert "content" in parsed, "ADF should have content field"

    def test_acli_update_sends_assignee(self, acli_mod: Any, acli_capture: Any) -> None:
        """AcliClient.update_issue() should support sending assignee updates.

        Bug 06a5: update_issue now pre-validates non-empty assignee values via
        validate_assignee_exists (REST /assignable/search) before dispatching
        to ACLI. Mock the validator so this test stays focused on ACLI flag
        propagation rather than re-testing the validation path.
        """
        client, captured_cmds, fake_run_acli = acli_capture

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(
                client, "validate_assignee_exists", return_value="acct-bob"
            ):
                client.update_issue("TEST-1", assignee="bob")

        assert len(captured_cmds) >= 1
        edit_cmd = captured_cmds[0]
        assert "--assignee" in edit_cmd, (
            f"ACLI edit command should include --assignee. Got: {edit_cmd}"
        )


# ============================================================================
# CONTRACT REGRESSION TESTS — added 2026-05-24 per ACLI audit (bug c916).
#
# Each test pins one ACLI invocation pattern to its EMPIRICALLY-VERIFIED shape
# against ACLI v1.3.18 + DIG project. A regression introducing a flag rename,
# missing --yes, payload-key typo, or shape drift WILL fail loudly in CI.
# Reference: epic 3a03 spec External Command Contract; bug c916 audit findings.
# ============================================================================


class TestAcliContractRegression:
    """Pin every ACLI invocation shape we use against its verified contract.

    Bugs prevented by this class:
      - c916-74a1-ed06-40e4 (add_label uses nonexistent --label flag)
      - 4fa9-0846-519e-4c30 (jira_project= kwarg omitted on AcliClient construction)
      - 5010-1c6a-9387-4b5b (priority dict stringified via str() fallback)
    """

    # --- ADD LABEL contract (the bug that motivated this class) -----------

    def test_add_label_uses_from_json_not_singular_label_flag(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """add_label MUST use --from-json with labelsToAdd, NEVER --label.

        Empirically verified 2026-05-24: 'acli jira workitem edit --label X' is
        rejected with 'unknown flag: --label'. The correct additive operation
        is the labelsToAdd field in the --from-json payload (per ACLI's own
        --generate-json output and Atlassian Community thread 3237097).
        Regression check: if anyone reintroduces --label, this test fails.
        """
        client, captured_cmds, fake_run_acli = acli_capture
        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            client.add_label("DIG-3802", "rebar-id:abc-123")

        assert len(captured_cmds) >= 1, "add_label must issue an ACLI command"
        cmd = captured_cmds[0]
        assert "--label" not in cmd, (
            f"add_label must NOT use --label (singular) — ACLI rejects it as "
            f"unknown flag. Got: {cmd}"
        )
        assert "--from-json" in cmd, (
            f"add_label must use --from-json for additive label semantics. Got: {cmd}"
        )
        assert "--yes" in cmd, (
            f"--from-json edit requires --yes to skip the confirmation prompt. "
            f"Got: {cmd}"
        )
        assert cmd[:4] == ["jira", "workitem", "edit", "--from-json"], (
            f"add_label command must start with 'jira workitem edit --from-json'. "
            f"Got: {cmd[:4]}"
        )

    def test_add_label_payload_uses_labelsToAdd_field(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """add_label's --from-json payload MUST use 'labelsToAdd' (additive op).

        Regression check: if anyone changes the payload key to 'labels'
        (set-replace, would destroy existing labels) or to a snake_case form,
        this test fails.
        """
        client, captured_cmds, fake_run_acli = acli_capture
        captured_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            captured_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.add_label("DIG-3802", "rebar-id:abc-123")

        assert captured_payloads, "add_label must json.dump a --from-json payload"
        payload = captured_payloads[0]
        assert payload.get("issues") == ["DIG-3802"], (
            f"Payload 'issues' must be a single-element list with the Jira key. "
            f"Got: {payload!r}"
        )
        assert payload.get("labelsToAdd") == ["rebar-id:abc-123"], (
            f"Payload must use 'labelsToAdd' (additive). 'labels' would be "
            f"set-replace and would destroy existing labels. Got: {payload!r}"
        )
        assert "labels" not in payload, (
            f"Payload must NOT contain 'labels' key (set-replace would destroy "
            f"existing labels). Got: {payload!r}"
        )

    def test_remove_label_payload_uses_labelsToRemove_field(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """remove_label MUST use 'labelsToRemove' (additive remove, not destructive)."""
        client, captured_cmds, fake_run_acli = acli_capture
        captured_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            captured_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.remove_label("DIG-3802", "obsolete-tag")

        assert captured_payloads
        payload = captured_payloads[0]
        assert payload.get("labelsToRemove") == ["obsolete-tag"], (
            f"remove_label must use 'labelsToRemove'. Got: {payload!r}"
        )
        assert payload.get("issues") == ["DIG-3802"]

    # --- CREATE contract --------------------------------------------------

    def test_create_with_priority_payload_uses_projectKey_not_project(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """CREATE --from-json payload MUST use 'projectKey' (NOT 'project').

        Regression check for bug 4fa9: the ACLI payload schema uses
        camelCase 'projectKey', not the bare 'project' field. Empty
        projectKey is rejected by ACLI with 'ProjectKey can't be null or
        blank'. Tests with priority=1 to force the --from-json path.
        """
        client, captured_cmds, fake_run_acli = acli_capture
        captured_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            captured_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.create_issue(
                    {"ticket_type": "task", "title": "x", "priority": 1}
                )

        assert captured_payloads
        payload = captured_payloads[0]
        assert "projectKey" in payload, (
            f"Payload must use 'projectKey' (camelCase per ACLI schema). "
            f"Got keys: {list(payload.keys())}"
        )
        assert payload["projectKey"], (
            "projectKey must be non-empty (ACLI rejects null/blank projectKey)"
        )
        assert "project" not in payload, (
            f"Payload must NOT use bare 'project' field (ACLI ignores it). "
            f"Got: {list(payload.keys())}"
        )

    def test_create_priority_payload_uses_additionalAttributes_priority_name(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """CREATE priority MUST live under additionalAttributes.priority.name.

        Empirically verified 2026-05-24: ACLI accepts
        additionalAttributes.priority = {"name": "High"} or {"id": "2"}.
        Top-level 'priority' on the create payload yields 'unknown field'.
        """
        client, captured_cmds, fake_run_acli = acli_capture
        captured_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            captured_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.create_issue(
                    {"ticket_type": "task", "title": "x", "priority": 1}
                )

        payload = captured_payloads[0]
        assert "priority" not in payload, (
            f"priority MUST NOT be a top-level field on the create payload. "
            f"ACLI rejects unknown top-level fields. Got: {list(payload.keys())}"
        )
        assert "additionalAttributes" in payload
        assert "priority" in payload["additionalAttributes"]
        assert "name" in payload["additionalAttributes"]["priority"], (
            f"priority sub-object must have 'name' field. "
            f"Got: {payload['additionalAttributes']['priority']!r}"
        )

    # --- DELETE contract --------------------------------------------------

    def test_delete_issue_uses_key_and_yes_flags(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """DELETE MUST use --key and --yes (skip interactive confirmation).

        delete_issue calls subprocess.run directly (not via _run_acli), so we
        patch subprocess.run and inspect the captured cmd argv.
        """
        from unittest.mock import MagicMock

        captured: list[list[str]] = []

        def fake_run(cmd: list[str], **kw: Any) -> Any:
            captured.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        client, _, _ = acli_capture
        with patch.object(acli_mod.subprocess, "run", side_effect=fake_run):
            client.delete_issue("DIG-3802")

        assert captured, "delete_issue must issue at least one subprocess call"
        cmd = captured[0]
        # acli_cmd prefix may be ["echo"] (test fixture) — strip it before assertion
        try:
            idx = cmd.index("jira")
        except ValueError:
            pytest.fail(f"DELETE cmd must contain 'jira'. Got: {cmd}")
        acli_args = cmd[idx:]
        assert acli_args[:3] == ["jira", "workitem", "delete"], (
            f"DELETE command must start with 'jira workitem delete'. Got: {acli_args[:3]}"
        )
        assert (
            "--key" in acli_args
            and acli_args[acli_args.index("--key") + 1] == "DIG-3802"
        )
        assert "--yes" in acli_args, (
            f"DELETE requires --yes or it hangs on the interactive prompt. "
            f"Got: {acli_args}"
        )

    # --- TRANSITION contract (module-level function) ----------------------

    def test_transition_issue_uses_rest_transitions_endpoint(
        self, acli_mod: Any
    ) -> None:
        """TRANSITION MUST use REST POST /rest/api/3/issue/{key}/transitions (bug 85a1 Gap 8).

        The previous ACLI-based ``workitem transition`` subcommand silently
        exited 0 on bogus transitions (bug 85a1 Gap 5 — the lying-success
        bug), so ``transition_issue`` was rewritten to use direct REST.
        This regression test pins the new contract: GET /transitions to
        list available, then POST /transitions with ``{"transition":
        {"id": "<id>"}}``. ACLI is NO LONGER called from this path.
        """
        from unittest.mock import patch as _patch

        rest_get_calls: list[str] = []
        rest_post_calls: list[tuple[str, dict]] = []

        def fake_get(self: Any, path: str) -> dict:
            rest_get_calls.append(path)
            return {
                "transitions": [
                    {"id": "31", "name": "Done", "to": {"name": "Done"}},
                ]
            }

        def fake_post(self: Any, path: str, body: dict) -> None:
            rest_post_calls.append((path, body))

        with (
            _patch.object(acli_mod.AcliClient, "_direct_rest_get", fake_get),
            _patch.object(acli_mod.AcliClient, "_direct_rest_post_raw", fake_post),
        ):
            acli_mod.transition_issue("DIG-3802", "Done")

        assert rest_get_calls == ["/rest/api/3/issue/DIG-3802/transitions"], (
            f"transition_issue must GET /transitions; got: {rest_get_calls}"
        )
        assert len(rest_post_calls) == 1, (
            f"expected one POST; got {len(rest_post_calls)}"
        )
        path, body = rest_post_calls[0]
        assert path == "/rest/api/3/issue/DIG-3802/transitions"
        assert body == {"transition": {"id": "31"}}


class TestAcliSanitizers:
    """Defend against untrusted user input in ticket summaries and labels.

    Local tickets may contain arbitrary user-supplied text (titles, tags).
    The reconciler must not crash mid-pass on a single oversize / malformed
    ticket, and must not pass invalid values to ACLI that would produce
    silent server-side mangling.
    """

    def test_sanitize_label_strips_whitespace(self, acli_mod: Any) -> None:
        assert acli_mod._sanitize_label("  foo  ") == "foo"

    def test_sanitize_label_rejects_internal_whitespace(self, acli_mod: Any) -> None:
        with pytest.raises(acli_mod.InvalidLabelError, match="whitespace"):
            acli_mod._sanitize_label("foo bar")

    def test_sanitize_label_rejects_tabs_and_newlines(self, acli_mod: Any) -> None:
        with pytest.raises(acli_mod.InvalidLabelError, match="whitespace"):
            acli_mod._sanitize_label("foo\tbar")
        with pytest.raises(acli_mod.InvalidLabelError, match="whitespace"):
            acli_mod._sanitize_label("foo\nbar")

    def test_sanitize_label_rejects_commas(self, acli_mod: Any) -> None:
        with pytest.raises(acli_mod.InvalidLabelError, match="comma"):
            acli_mod._sanitize_label("foo,bar")

    def test_sanitize_label_rejects_empty(self, acli_mod: Any) -> None:
        with pytest.raises(acli_mod.InvalidLabelError, match="empty"):
            acli_mod._sanitize_label("")
        with pytest.raises(acli_mod.InvalidLabelError, match="empty"):
            acli_mod._sanitize_label("   ")

    def test_sanitize_label_rejects_oversize(self, acli_mod: Any) -> None:
        with pytest.raises(acli_mod.InvalidLabelError, match="255-char"):
            acli_mod._sanitize_label("a" * 256)

    def test_sanitize_label_accepts_exact_max_length(self, acli_mod: Any) -> None:
        """Jira's label max is inclusive 255 ('no more than 255 characters').

        A 255-char label MUST be accepted (boundary-exact). This test catches
        an off-by-one in _JIRA_LABEL_MAX_CHARS that would silently reject
        a valid label.
        """
        max_label = "a" * 255
        assert acli_mod._sanitize_label(max_label) == max_label

    def test_sanitize_label_accepts_unicode_word(self, acli_mod: Any) -> None:
        # Unicode word chars are fine — Jira accepts them.
        assert acli_mod._sanitize_label("café-tag") == "café-tag"

    def test_sanitize_label_rejects_non_str(self, acli_mod: Any) -> None:
        with pytest.raises(acli_mod.InvalidLabelError, match="must be str"):
            acli_mod._sanitize_label(123)

    def test_sanitize_summary_truncates_oversize(self, acli_mod: Any) -> None:
        long_summary = "x" * 300
        result = acli_mod._sanitize_summary(long_summary)
        # Inclusive max is 254 (Jira's error is "less than 255").
        assert len(result) <= 254
        assert result.endswith(" [truncated]")

    def test_sanitize_summary_truncates_at_255_boundary(self, acli_mod: Any) -> None:
        """Jira's summary error is 'must be less than 255' (strict less-than).

        A 255-char summary MUST be rejected by Jira, so our sanitizer MUST
        truncate it. This boundary-exact test catches the off-by-one that
        the prior implementation had (_JIRA_SUMMARY_MAX_CHARS = 255 silently
        passed 255-char titles through to Jira, which then rejected them
        and crashed the reconciler pass). Source: Atlassian Community
        thread 989632 + tenable/integration-jira-cloud#322.
        """
        summary_255 = "x" * 255
        result = acli_mod._sanitize_summary(summary_255)
        # MUST be truncated — passing 255 chars through is a Jira API rejection.
        assert len(result) <= 254, (
            f"255-char summary must be truncated to <=254 to satisfy Jira's "
            f"'less than 255' rule. Got length {len(result)}: {result!r}"
        )
        assert result.endswith(" [truncated]")

    def test_sanitize_summary_accepts_254_char_max(self, acli_mod: Any) -> None:
        """Exactly 254 chars is the inclusive max — must be preserved verbatim."""
        summary_254 = "y" * 254
        result = acli_mod._sanitize_summary(summary_254)
        assert result == summary_254, (
            f"254-char summary (inclusive max) must pass through unchanged. "
            f"Got length {len(result)}, expected 254."
        )

    def test_sanitize_summary_preserves_short_input(self, acli_mod: Any) -> None:
        assert acli_mod._sanitize_summary("short title") == "short title"

    def test_sanitize_summary_strips_whitespace(self, acli_mod: Any) -> None:
        assert acli_mod._sanitize_summary("  hello  ") == "hello"

    def test_sanitize_summary_rejects_empty(self, acli_mod: Any) -> None:
        with pytest.raises(ValueError, match="empty"):
            acli_mod._sanitize_summary("")
        with pytest.raises(ValueError, match="empty"):
            acli_mod._sanitize_summary("   ")

    def test_add_label_rejects_label_with_whitespace(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """The sanitizer must intercept invalid labels BEFORE the ACLI call."""
        client, captured_cmds, fake_run_acli = acli_capture
        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with pytest.raises(acli_mod.InvalidLabelError):
                client.add_label("DIG-3802", "label with space")
        assert len(captured_cmds) == 0, (
            "Invalid label must be rejected client-side; no ACLI call should fire"
        )

    def test_create_issue_truncates_oversize_title(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """Oversize titles must be truncated, not crash the reconciler."""
        client, captured_cmds, fake_run_acli = acli_capture
        captured_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            captured_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.create_issue(
                    {
                        "ticket_type": "task",
                        "title": "y" * 300,
                        "priority": 1,
                    }
                )
        assert captured_payloads
        summary = captured_payloads[0]["summary"]
        assert len(summary) <= 255
        assert summary.endswith(" [truncated]")
