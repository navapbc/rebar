#!/usr/bin/env python3
"""Measure A3 baseline normalization against the real tickets-store corpus.

The driver reads ``bindings.json`` from a git ref, projects only description,
priority, and status, and then runs the production outbound comparison twice:

* current checkout: raw baseline versus normalized baseline;
* an explicitly named pre-A3 revision: raw baseline versus normalized baseline.

Each comparison uses the real reduced local ticket and its stored baseline as the
observed remote state.  The worker subprocess keeps revision imports isolated so
the rollback oracle genuinely executes the pre-A3 implementation.
"""

from __future__ import annotations

import argparse
import copy
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

_BINDINGS_PATH = ".bridge_state/bindings.json"
_PROJECTED_FIELDS = ("description", "priority", "status")


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _load_bindings(repo: Path, ref: str) -> tuple[bytes, dict[str, Any]]:
    raw = _git(repo, "show", f"{ref}:{_BINDINGS_PATH}")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("bindings"), dict):
        raise ValueError(f"{ref}:{_BINDINGS_PATH} is not a binding-store object")
    return raw, parsed


def _load_tickets(repo: Path) -> dict[str, dict[str, Any]]:
    sys.path.insert(0, str(repo / "src"))
    import rebar

    # Bindings can legitimately include the non-graph ``code_review`` artifact
    # type, which the public work-ticket listing excludes.  The native reducer is
    # the complete store census and keeps those rows in the corpus.
    tickets = rebar.reduce_all_tickets(
        repo / ".tickets-tracker",
        exclude_archived=False,
        exclude_deleted=False,
        exclude_session_logs=False,
    )
    return {ticket["ticket_id"]: dict(ticket) for ticket in tickets}


def _normalized_store(repo: Path, raw_store: dict[str, Any]) -> dict[str, Any]:
    sys.path.insert(0, str(repo / "src" / "rebar" / "_engine"))
    from rebar_reconciler.inbound_fields import normalize_baseline_value

    normalized = copy.deepcopy(raw_store)
    for entry in normalized["bindings"].values():
        baseline = entry.get("baseline")
        if not isinstance(baseline, dict):
            continue
        for field in _PROJECTED_FIELDS:
            if field in baseline:
                baseline[field] = normalize_baseline_value(field, baseline[field])
    return normalized


def _serialized(store: dict[str, Any]) -> bytes:
    return (json.dumps(store, indent=2, sort_keys=True) + "\n").encode()


def _assignee_delta(raw_store: dict[str, Any], normalized_store: dict[str, Any]) -> list[str]:
    changed: list[str] = []
    for local_id, raw_entry in raw_store["bindings"].items():
        normalized_entry = normalized_store["bindings"][local_id]
        raw_baseline = raw_entry.get("baseline")
        normalized_baseline = normalized_entry.get("baseline")
        if not isinstance(raw_baseline, dict) or not isinstance(normalized_baseline, dict):
            continue
        raw_value = ("assignee" in raw_baseline, raw_baseline.get("assignee"))
        normalized_value = (
            "assignee" in normalized_baseline,
            normalized_baseline.get("assignee"),
        )
        if raw_value != normalized_value:
            changed.append(local_id)
    return changed


class _CorpusStore:
    def __init__(self, store: dict[str, Any]) -> None:
        self._bindings = store["bindings"]
        self._reverse = store.get("reverse", {})

    def get_baseline(self, local_id: str) -> dict[str, Any] | None:
        baseline = self._bindings.get(local_id, {}).get("baseline")
        return baseline if isinstance(baseline, dict) else None

    def is_pending(self, local_id: str) -> bool:
        return self._bindings.get(local_id, {}).get("state") == "pending"

    def get_jira_key(self, local_id: str) -> str | None:
        value = self._bindings.get(local_id, {}).get("jira_key")
        return value if isinstance(value, str) else None

    def get_local_id(self, jira_key: str) -> str | None:
        value = self._reverse.get(jira_key)
        return value if isinstance(value, str) else None


def _outcomes(
    store_data: dict[str, Any],
    remote_store: dict[str, Any],
    tickets: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    from rebar_reconciler.adapters.jira.backend import JiraBackend
    from rebar_reconciler.outbound_field_diff import compute_update_fields

    backend = JiraBackend(transport=object())
    store = _CorpusStore(store_data)
    local_ticket_types = {
        local_id: str(ticket.get("ticket_type", "task")) for local_id, ticket in tickets.items()
    }
    outcomes: dict[str, Any] = {}
    missing_tickets: list[str] = []
    errors: list[dict[str, str]] = []

    for local_id, remote_entry in sorted(remote_store["bindings"].items()):
        baseline = remote_entry.get("baseline")
        if not isinstance(baseline, dict):
            continue
        ticket = tickets.get(local_id)
        if ticket is None:
            missing_tickets.append(local_id)
            continue
        jira_key = str(remote_entry.get("jira_key") or "")
        conflicts: list[tuple[str, str]] = []
        dropped: list[tuple[str, str]] = []
        try:
            fields = compute_update_fields(
                ticket,
                baseline,
                inbound_mapper=backend.inbound,
                outbound_mapper=backend.outbound,
                binding_store=store,
                local_id=local_id,
                jira_key=jira_key,
                local_ticket_types=local_ticket_types,
                conflict_sink=conflicts,
                dropped_field_sink=dropped,
            )
        except Exception as exc:  # noqa: BLE001 - corpus reports every incompatible row
            errors.append(
                {
                    "local_id": local_id,
                    "type": type(exc).__name__,
                    "message": str(exc),
                }
            )
            continue
        outcomes[local_id] = {
            "fields": fields,
            "conflicts": conflicts,
            "dropped": dropped,
        }

    return {
        "outcomes": outcomes,
        "missing_tickets": missing_tickets,
        "errors": errors,
    }


def _worker(corpus_path: Path) -> int:
    corpus = json.loads(corpus_path.read_text())
    raw = _outcomes(corpus["raw_store"], corpus["raw_store"], corpus["tickets"])
    normalized = _outcomes(corpus["normalized_store"], corpus["raw_store"], corpus["tickets"])
    all_ids = sorted(set(raw["outcomes"]) | set(normalized["outcomes"]))
    deltas = [
        local_id
        for local_id in all_ids
        if raw["outcomes"].get(local_id) != normalized["outcomes"].get(local_id)
    ]
    result = {
        "compared": len(all_ids),
        "mutation_deltas": len(deltas),
        "delta_sample": deltas[:20],
        "raw_missing_tickets": raw["missing_tickets"],
        "normalized_missing_tickets": normalized["missing_tickets"],
        "raw_errors": raw["errors"],
        "normalized_errors": normalized["errors"],
    }
    print(json.dumps(result, sort_keys=True))  # noqa: T201 - machine-readable worker protocol
    return 0


def _extract_revision(repo: Path, ref: str, destination: Path) -> str:
    resolved = _git(repo, "rev-parse", ref).decode().strip()
    archive = _git(repo, "archive", "--format=tar", resolved)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        tar.extractall(destination, filter="data")
    return resolved


def _run_worker(script: Path, corpus_path: Path, source_root: Path) -> dict[str, Any]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(source_root / "src" / "rebar" / "_engine"),
            str(source_root / "src"),
        ]
    )
    completed = subprocess.run(
        [sys.executable, str(script), "--worker", str(corpus_path)],
        check=True,
        capture_output=True,
        text=True,
        cwd=source_root,
        env=env,
    )
    return json.loads(completed.stdout)


def run(repo: Path, store_ref: str, pre_a3_ref: str) -> dict[str, Any]:
    raw_bytes, raw_store = _load_bindings(repo, store_ref)
    normalized_store = _normalized_store(repo, raw_store)
    normalized_bytes = _serialized(normalized_store)
    tickets = _load_tickets(repo)
    assignee_deltas = _assignee_delta(raw_store, normalized_store)

    corpus = {
        "raw_store": raw_store,
        "normalized_store": normalized_store,
        "tickets": tickets,
    }
    script = Path(__file__).resolve()
    with tempfile.TemporaryDirectory(prefix="rebar-a3-corpus-") as temp_name:
        temp = Path(temp_name)
        corpus_path = temp / "corpus.json"
        corpus_path.write_text(json.dumps(corpus, sort_keys=True))
        forward = _run_worker(script, corpus_path, repo)
        pre_a3_root = temp / "pre-a3"
        pre_a3_root.mkdir()
        pre_a3_sha = _extract_revision(repo, pre_a3_ref, pre_a3_root)
        rollback = _run_worker(script, corpus_path, pre_a3_root)

    raw_size = len(raw_bytes)
    normalized_size = len(normalized_bytes)
    reduction = 1.0 - (normalized_size / raw_size)
    baseline_count = sum(
        isinstance(entry.get("baseline"), dict) for entry in raw_store["bindings"].values()
    )
    error_count = sum(
        len(result[key])
        for result in (forward, rollback)
        for key in (
            "raw_missing_tickets",
            "normalized_missing_tickets",
            "raw_errors",
            "normalized_errors",
        )
    )
    passed = (
        forward["mutation_deltas"] == 0
        and rollback["mutation_deltas"] == 0
        and not assignee_deltas
        and reduction >= 0.50
        and error_count == 0
        and forward["compared"] == rollback["compared"] == baseline_count
    )
    return {
        "store_ref": store_ref,
        "store_sha": _git(repo, "rev-parse", store_ref).decode().strip(),
        "pre_a3_sha": pre_a3_sha,
        "bindings": len(raw_store["bindings"]),
        "baselines": baseline_count,
        "raw_bytes": raw_size,
        "normalized_bytes": normalized_size,
        "size_reduction_percent": round(reduction * 100, 3),
        "assignee_shape_deltas": len(assignee_deltas),
        "assignee_delta_sample": assignee_deltas[:20],
        "forward": forward,
        "rollback": rollback,
        "passed": passed,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--store-ref", default="origin/tickets")
    parser.add_argument(
        "--pre-a3-ref",
        help="required git revision containing the pre-A3 comparison implementation",
    )
    parser.add_argument("--worker", type=Path, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.worker is not None:
        return _worker(args.worker)
    if not args.pre_a3_ref:
        raise SystemExit("--pre-a3-ref is required")
    result = run(args.repo.resolve(), args.store_ref, args.pre_a3_ref)
    print(json.dumps(result, indent=2, sort_keys=True))  # noqa: T201 - experiment artifact
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
