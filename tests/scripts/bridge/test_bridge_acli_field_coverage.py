"""Test Jira bridge field extraction, ACLI commands, and input sanitizers.

Field tests pin values sent from ticket data. Command tests preserve ACLI arguments and
payloads for project keys, priorities, labels, and confirmation flags. Sanitizer tests cover
malformed and oversized summaries and labels.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


class TestAcliClientCreateFieldExtraction:
    """Test fields forwarded by ``AcliClient.create_issue()``."""

    def test_acli_create_sends_summary(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """Check summary placement in flag and JSON creation paths.

        Priority selects JSON input. Other creation requests use ``--summary``.
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
        """Extract ``priority.name`` from Jira REST priority objects.

        The reconciler forwards snapshot priority objects. ACLI requires the nested name
        instead of a Python representation of the object.
        """
        client, _captured_cmds, fake_run_acli = acli_capture

        # The fetcher forwards this Jira REST shape through the differ and applier.
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
        client, _captured_cmds, fake_run_acli = acli_capture
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
        client, _captured_cmds, fake_run_acli = acli_capture
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
        """Resolve the assignee and forward its account ID to ACLI."""
        client, captured_cmds, fake_run_acli = acli_capture
        client._direct_rest_get = MagicMock(
            return_value=[
                {
                    "accountId": "alice-acct",
                    "displayName": "alice",
                    "emailAddress": "alice@example.com",
                }
            ]
        )

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
        # The RESOLVED accountId is forwarded, not the raw handle (bug 544e).
        assert create_cmd[create_cmd.index("--assignee") + 1] == "alice-acct"


class TestAcliClientUpdateFieldExtraction:
    """Test which fields AcliClient.update_issue() sends for non-status field updates."""

    def test_acli_update_routes_priority_to_rest(self, acli_mod: Any, acli_capture: Any) -> None:
        """Route priority updates through the Jira REST endpoint."""
        client, captured_cmds, fake_run_acli = acli_capture

        with (
            patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli),
            patch.object(acli_mod.acli_cli_ops, "update_priority") as mock_priority,
        ):
            result = client.update_issue("TEST-1", priority="High")

        # Priority-only updates must not invoke ACLI.
        assert len(captured_cmds) == 0, (
            f"No ACLI command should be issued for priority-only update. Got: {captured_cmds}"
        )
        assert result == {"key": "TEST-1"}

        # The REST path does not receive the client's ACLI argument prefix.
        mock_priority.assert_called_once_with("TEST-1", "High")

    def test_acli_update_sends_description(self, acli_mod: Any, acli_capture: Any) -> None:
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
        """Validate the assignee before forwarding its account ID to ACLI."""
        client, captured_cmds, fake_run_acli = acli_capture

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(client, "validate_assignee_exists", return_value="acct-bob"):
                client.update_issue("TEST-1", assignee="bob")

        assert len(captured_cmds) >= 1
        edit_cmd = captured_cmds[0]
        assert "--assignee" in edit_cmd, (
            f"ACLI edit command should include --assignee. Got: {edit_cmd}"
        )


# ACLI command and payload contracts.


class TestAcliContractRegression:
    """Pin ACLI argument and payload shapes used by the bridge."""

    # Add labels without replacing the existing set.

    def test_add_label_uses_from_json_not_singular_label_flag(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """Use ``labelsToAdd`` because ACLI rejects the singular ``--label`` flag.

        The JSON operation follows ACLI-generated output and Atlassian Community thread
        3237097.
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
            f"--from-json edit requires --yes to skip the confirmation prompt. Got: {cmd}"
        )
        assert cmd[:4] == ["jira", "workitem", "edit", "--from-json"], (
            f"add_label command must start with 'jira workitem edit --from-json'. Got: {cmd[:4]}"
        )

    def test_add_label_payload_uses_labelsToAdd_field(
        self, acli_mod: Any, acli_capture: Any
    ) -> None:
        """Use ``labelsToAdd`` to preserve existing labels during additive updates."""
        client, _captured_cmds, fake_run_acli = acli_capture
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
            f"Payload 'issues' must be a single-element list with the Jira key. Got: {payload!r}"
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
        client, _captured_cmds, fake_run_acli = acli_capture
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

    def test_create_with_priority_payload_uses_projectKey_not_project(
        self, acli_mod: Any, acli_capture: Any, mock_jira_verify: Any
    ) -> None:
        """Use ``projectKey`` in JSON creation payloads.

        ACLI rejects empty ``projectKey`` values and does not accept ``project`` for this
        field. Priority selects the JSON creation path.
        """
        client, _captured_cmds, fake_run_acli = acli_capture
        captured_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            captured_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.create_issue({"ticket_type": "task", "title": "x", "priority": 1})

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
        """Place creation priority under ``additionalAttributes.priority.name``.

        ACLI accepts name or ID priority objects and rejects a top-level ``priority`` field.
        """
        client, _captured_cmds, fake_run_acli = acli_capture
        captured_payloads: list[Any] = []
        original_dump = json.dump

        def capturing_dump(obj: Any, fp: Any, **kw: Any) -> None:
            captured_payloads.append(obj)
            original_dump(obj, fp, **kw)

        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            with patch.object(acli_mod.json, "dump", side_effect=capturing_dump):
                client.create_issue({"ticket_type": "task", "title": "x", "priority": 1})

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

    def test_delete_issue_uses_key_and_yes_flags(self, acli_mod: Any, acli_capture: Any) -> None:
        """Pass ``--key`` and ``--yes`` through the shared ACLI subprocess runner."""
        from unittest.mock import MagicMock

        captured: list[list[str]] = []

        def fake_run_acli(
            cmd: list[str],
            *,
            acli_cmd: list[str] | None = None,
            retry_on_timeout: bool = False,
            call_timeout: float | None = None,
        ) -> Any:
            captured.append(cmd)
            result = MagicMock()
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
            return result

        client, _, _ = acli_capture
        with patch.object(acli_mod.acli_subprocess, "_run_acli", side_effect=fake_run_acli):
            client.delete_issue("DIG-3802")

        assert captured, "delete_issue must issue at least one subprocess call"
        cmd = captured[0]
        # _run_acli receives the argv WITHOUT the acli_cmd prefix.
        try:
            idx = cmd.index("jira")
        except ValueError:
            pytest.fail(f"DELETE cmd must contain 'jira'. Got: {cmd}")
        acli_args = cmd[idx:]
        assert acli_args[:3] == ["jira", "workitem", "delete"], (
            f"DELETE command must start with 'jira workitem delete'. Got: {acli_args[:3]}"
        )
        assert "--key" in acli_args and acli_args[acli_args.index("--key") + 1] == "DIG-3802"
        assert "--yes" in acli_args, (
            f"DELETE requires --yes or it hangs on the interactive prompt. Got: {acli_args}"
        )

    def test_transition_issue_uses_rest_transitions_endpoint(self, acli_mod: Any) -> None:
        """Use Jira REST to list and apply issue transitions.

        The operation reads ``/transitions`` before posting the selected transition ID. It
        does not invoke ACLI.
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
        assert len(rest_post_calls) == 1, f"expected one POST; got {len(rest_post_calls)}"
        path, body = rest_post_calls[0]
        assert path == "/rest/api/3/issue/DIG-3802/transitions"
        assert body == {"transition": {"id": "31"}}


class TestAcliSanitizers:
    """Validate user-supplied summaries and labels before reconciliation.

    Invalid input must fail before ACLI. Oversized summaries must be shortened to Jira's
    limits.
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
        """Accept Jira labels at the inclusive 255-character limit."""
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
        """Shorten summaries at Jira's exclusive 255-character boundary.

        Jira requires fewer than 255 characters. This limit follows Atlassian Community thread
        989632 and ``tenable/integration-jira-cloud#322``.
        """
        summary_255 = "x" * 255
        result = acli_mod._sanitize_summary(summary_255)
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
        client, _captured_cmds, fake_run_acli = acli_capture
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
