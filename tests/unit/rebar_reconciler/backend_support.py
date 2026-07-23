"""Shared test doubles for the reconciler backend-port contract suite (S2, epic bbf1).

Importable (non-test) helpers so the backend-agnostic contract tests can run against
BOTH the real ``JiraBackend`` (with an injected fake transport) and an in-memory
``FakeBackend``. Neither double encodes the Jira characterization oracle — that lives
in the held-out characterization tests.

The engine dir is placed on ``sys.path`` by the package conftest, so the reconciler
modules import flat (``from rebar_reconciler import ...``).
"""

from __future__ import annotations

from typing import Any

from rebar_reconciler._backend import RemoteRef


class FakeTransport:
    """Minimal in-memory ``TicketTransport`` + link/comment surface for tests.

    Records calls so behavioural assertions (e.g. "links synced?") can observe them.
    """

    def __init__(self) -> None:
        self.store: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._counter = 0

    def create_issue(self, ticket_data: dict[str, Any]) -> dict[str, Any]:
        self._counter += 1
        key = f"FAKE-{self._counter}"
        self.store[key] = dict(ticket_data)
        self.calls.append(("create_issue", (ticket_data,)))
        return {"key": key}

    def get_issue(self, remote_id: str) -> dict[str, Any]:
        self.calls.append(("get_issue", (remote_id,)))
        return {"key": remote_id, "fields": self.store.get(remote_id, {})}

    def update_issue(self, remote_id: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update_issue", (remote_id,)))
        self.store.setdefault(remote_id, {}).update(kwargs)
        return {"key": remote_id}

    def transition_issue_by_name(self, remote_id: str, target_status: str) -> None:
        self.calls.append(("transition_issue_by_name", (remote_id, target_status)))

    def add_label(self, remote_id: str, label: str) -> None:
        self.calls.append(("add_label", (remote_id, label)))

    def search_issues(
        self, jql: str, start_at: int = 0, max_results: int = 50
    ) -> list[dict[str, Any]]:
        self.calls.append(("search_issues", (jql,)))
        return []

    # link + comment surface (JiraBackend delegates its capability methods here)
    def set_relationship(
        self, from_id: str, to_id: str, link_type: str = "Blocks"
    ) -> dict[str, Any]:
        self.calls.append(("set_relationship", (from_id, to_id, link_type)))
        return {}

    def get_issuelinks_map(self, project_key: str) -> dict[str, Any]:
        self.calls.append(("get_issuelinks_map", (project_key,)))
        return {}

    def add_comment(self, remote_id: str, body: str) -> dict[str, Any]:
        self.calls.append(("add_comment", (remote_id, body)))
        return {"id": "1"}

    def get_comment_map(self, project_key: str) -> dict[str, Any]:
        self.calls.append(("get_comment_map", (project_key,)))
        return {}


# ---------------------------------------------------------------------------
# FakeBackend — an in-memory Backend that deliberately does NOT support links or
# incremental fetch, so the capability-detection contract has a negative case.
# ---------------------------------------------------------------------------


class _FakeOutbound:
    def map_local_to_remote(
        self,
        ticket: dict[str, Any],
        binding_store: Any | None = None,
        local_ticket_types: dict[str, str] | None = None,
        emit_detach_clear: bool = False,
    ) -> dict[str, Any]:
        return {
            "summary": ticket.get("title") or "",
            "description": ticket.get("description") or "",
            "priority": ticket.get("priority", 2),
            "status": ticket.get("status", "open"),
        }


class _FakeInbound:
    def map_remote_to_local(self, remote_fields: dict[str, Any]) -> dict[str, Any]:
        return {
            "title": remote_fields.get("summary", ""),
            "description": remote_fields.get("description", ""),
            "priority": remote_fields.get("priority", 2),
            "status": remote_fields.get("status", "open"),
        }


class _FakeSanitizer:
    _MAX = 50

    def sanitize_label(self, label: str) -> str:
        s = label.strip()
        if not s or any(c.isspace() for c in s):
            raise ValueError(f"bad label: {label!r}")
        return s

    def sanitize_summary(self, summary: str) -> str:
        return summary.strip()[: self._MAX]

    def sanitize_description(self, description: str) -> str:
        return description[: self._MAX]

    def sanitize_comment(self, body: str) -> str:
        return body[: self._MAX]


class _FakeIdentity:
    _PREFIX = "fake-id:"

    def format_label(self, local_id: str) -> str:
        return f"{self._PREFIX}{local_id}"

    def parse_label(self, label: str) -> str | None:
        if label.startswith(self._PREFIX) and label[len(self._PREFIX) :].strip():
            return label[len(self._PREFIX) :]
        return None

    def is_identity_label(self, label: str) -> bool:
        return self.parse_label(label) is not None


class FakeBackend:
    """In-memory backend implementing the five role Protocols but NO capabilities."""

    vendor = "fake"
    # Facade project accessors (ticket 4af8). The in-memory fake carries a fixed
    # scope: ``project`` is its effective (defaulted) write scope; ``query_project``
    # its raw read scope. Distinct from JiraBackend's config-resolved values, but
    # enough to satisfy the runtime-checkable ``Backend`` facade contract.
    project = "FAKE"
    query_project = "FAKE"

    def __init__(self) -> None:
        self.transport = FakeTransport()
        self.outbound = _FakeOutbound()
        self.inbound = _FakeInbound()
        self.sanitizer = _FakeSanitizer()
        self.identity = _FakeIdentity()

    def remote_ref(self, remote_id: str) -> RemoteRef:
        return RemoteRef(vendor=self.vendor, instance="test", remote_id=remote_id)
