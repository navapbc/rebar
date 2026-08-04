"""Targeted, DETERMINISTIC probe: can an Epic Link be CLEARED, and is `parent` screenable?

Bug 1019 / epic e369. Two questions block the last reachable live-suite red
(`test_outbound_clear_parent_round_trips`, re-homed to the Epic Link path per ticket 37e7's
approved ruling), and both are cheap:

  Q1  Does clearing the Epic Link custom field actually take effect on Jira DC 8.17.1?
      The capability map measured that WRITING it works (req-0040/0041). Clearing it is
      UNTESTED, and `set_parent`'s non-sub-task branch has no read-back verification, so a
      silent no-op there is invisible today.

  Q2  Can `parent` be put on a screen at all? The sub-task `parent` update-verb form fails with
      a screen-shaped error, which made screen provisioning look promising. Desk research says
      no `OrderableField` implementation for Parent exists in Jira Server 8.x, so it cannot be
      screened — but that is INFERENCE from docs, and this epic has been burned by inference.

WHY THIS IS NOT THE CAPABILITY MAP. The map is an agentic Opus pass over a broad checklist and
takes a long, billable run. These are four REST calls with predetermined shapes, so this script
is plain deterministic urllib: no LLM, no agent, no re-answering of anything the map already
settled. Running it does not re-run, invalidate, or supersede the committed map.

WHY IT RUNS IN CI. The harness image is linux/amd64-only and cannot boot locally
(tests/external/live_jira_dc/README.md), so the probe rides the same digest-pinned image the
map and the live suite use, booted by its own workflow.

Output: a JSON evidence file (every request/response, verbatim) plus a one-line verdict per
question on stdout. It answers questions; it changes nothing in the repo.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import pathlib
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira").rstrip("/")
USER = os.environ.get("JIRA_DC_USER", "admin")
PASSWORD = os.environ.get("JIRA_DC_PASSWORD", "admin")
OUT_DIR = pathlib.Path(os.environ.get("JIRA_DC_PROBE_OUTPUT_DIR", "jira-dc-epic-link-probe"))

# `logging`, not `print`: `scripts/` is not on the T201 allowlist, and the allowlist was
# deliberately closed out (pyproject.toml) rather than grown.
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("jira-dc-epic-link-probe")

# The template the harness itself pins (tests/external/live_jira_dc/conftest.py) — the probe must
# provision the SAME shape of project the suite runs against, or its answer is about some other
# environment.
PROJECT_TEMPLATE = "com.pyxis.greenhopper.jira:gh-scrum-template"

_EVIDENCE: list[dict] = []


def _req(path: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, object]:
    """One raw REST round trip, recorded verbatim into the evidence log."""
    url = f"{BASE}{path}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)  # noqa: S310 — fixed localhost harness
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:  # noqa: S310
            raw = resp.read().decode() or ""
            status = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode() or ""
        status = exc.code
    except Exception as exc:  # noqa: BLE001 — a transport error is itself evidence
        _EVIDENCE.append({"method": method, "path": path, "payload": payload, "error": repr(exc)})
        return 0, repr(exc)
    try:
        parsed = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        parsed = raw
    _EVIDENCE.append(
        {"method": method, "path": path, "payload": payload, "status": status, "response": parsed}
    )
    return status, parsed


def _field_id(name: str) -> str | None:
    status, body = _req("/rest/api/2/field")
    if status != 200 or not isinstance(body, list):
        # Say WHY rather than collapsing an unreachable/unauthorised instance and a genuinely
        # absent field into the same `None`. The first probe run reported only
        # "Epic Link=None Epic Name=None", which does not distinguish the two.
        logger.info(
            "  field inventory unusable: GET /rest/api/2/field -> HTTP %s, body type %s",
            status,
            type(body).__name__,
        )
        return None
    found = next(
        (str(f.get("id")) for f in body if isinstance(f, dict) and f.get("name") == name), None
    )
    if found is None:
        logger.info(
            "  field %r absent from an inventory of %d field(s); names present: %s",
            name,
            len(body),
            sorted({str(f.get("name")) for f in body if isinstance(f, dict)})[:40],
        )
    return found


def _create_issue(project: str, issuetype: str, summary: str, extra: dict | None = None) -> str:
    fields = {"project": {"key": project}, "issuetype": {"name": issuetype}, "summary": summary}
    fields.update(extra or {})
    status, body = _req("/rest/api/2/issue", method="POST", payload={"fields": fields})
    if status != 201 or not isinstance(body, dict):
        raise SystemExit(f"PROBE SETUP FAILED: could not create {issuetype}: {status} {body!r}")
    return str(body["key"])


def main() -> int:
    verdicts: dict[str, str] = {}

    # ── setup ────────────────────────────────────────────────────────────────────────────────
    epic_link = _field_id("Epic Link")
    epic_name = _field_id("Epic Name")
    if not epic_link or not epic_name:
        raise SystemExit(f"PROBE SETUP FAILED: Epic Link={epic_link!r} Epic Name={epic_name!r}")

    key = "ELP"
    _req(f"/rest/api/2/project/{key}", method="DELETE")  # idempotent: ignore the outcome
    status, body = _req(
        "/rest/api/2/project",
        method="POST",
        payload={
            "key": key,
            "name": "epic-link clear probe",
            "lead": USER,
            "projectTypeKey": "software",
            "projectTemplateKey": PROJECT_TEMPLATE,
        },
    )
    if status != 201:
        raise SystemExit(f"PROBE SETUP FAILED: project create -> {status} {body!r}")

    epic = _create_issue(key, "Epic", "probe epic", {epic_name: "probe epic"})
    child = _create_issue(key, "Task", "probe child")

    # Precondition: the Epic Link must LAND before its clearing means anything.
    _req(f"/rest/api/2/issue/{child}", method="PUT", payload={"fields": {epic_link: epic}})
    status, body = _req(f"/rest/api/2/issue/{child}?fields={epic_link}")
    landed = (body.get("fields") or {}).get(epic_link) if isinstance(body, dict) else None
    if landed != epic:
        raise SystemExit(f"PROBE SETUP FAILED: Epic Link never reached {child}: {landed!r}")
    verdicts["setup_epic_link_write"] = f"OK — {child} Epic Link = {landed}"

    # ── Q1a: clear via the fields form ───────────────────────────────────────────────────────
    s1, b1 = _req(f"/rest/api/2/issue/{child}", method="PUT", payload={"fields": {epic_link: None}})
    status, body = _req(f"/rest/api/2/issue/{child}?fields={epic_link}")
    after1 = (body.get("fields") or {}).get(epic_link) if isinstance(body, dict) else "<unread>"
    verdicts["q1a_fields_null"] = (
        f"PUT fields.{epic_link}=null -> HTTP {s1}; read-back = {after1!r} — "
        + ("CLEARED" if not after1 else "IGNORED (still set)")
    )

    # ── Q1b: the update-verb form, only if the first did not clear ───────────────────────────
    if after1:
        s2, b2 = _req(
            f"/rest/api/2/issue/{child}",
            method="PUT",
            payload={"update": {epic_link: [{"set": None}]}},
        )
        status, body = _req(f"/rest/api/2/issue/{child}?fields={epic_link}")
        after2 = (body.get("fields") or {}).get(epic_link) if isinstance(body, dict) else "<unread>"
        verdicts["q1b_update_set_null"] = (
            f"PUT update.{epic_link}=[{{set:null}}] -> HTTP {s2}; read-back = {after2!r} — "
            + ("CLEARED" if not after2 else "IGNORED (still set)")
        )
    else:
        verdicts["q1b_update_set_null"] = "not attempted — the fields form already cleared it"

    # ── Q2: is `parent` a screenable field at all? (read-only) ───────────────────────────────
    status, fields = _req("/rest/api/2/field")
    named_parent = (
        [f for f in fields if isinstance(f, dict) and str(f.get("id")) == "parent"]
        if isinstance(fields, list)
        else []
    )
    verdicts["q2_parent_in_field_inventory"] = (
        f"{len(named_parent)} field(s) with id 'parent' in /rest/api/2/field — "
        + ("PRESENT (screen provisioning may be revivable)" if named_parent else "ABSENT")
    )
    # Also record what IS there under a parent-ish name, so the Parent Link confusion is on record.
    parentish = (
        [
            {"id": f.get("id"), "name": f.get("name")}
            for f in fields
            if isinstance(f, dict) and "parent" in str(f.get("name", "")).lower()
        ]
        if isinstance(fields, list)
        else []
    )
    verdicts["q2_parent_named_fields"] = json.dumps(parentish)

    # ── teardown + evidence ──────────────────────────────────────────────────────────────────
    _req(f"/rest/api/2/project/{key}", method="DELETE")

    _write_evidence(verdicts)

    logger.info("=== JIRA DC EPIC-LINK CLEAR PROBE ===")
    for k, v in verdicts.items():
        logger.info("  %s: %s", k, v)
    logger.info("evidence: %d request/response pairs -> %s/evidence.json", len(_EVIDENCE), OUT_DIR)
    return 0


def _write_evidence(verdicts: dict[str, str]) -> None:
    """Persist verdicts + every recorded round trip. Safe to call on ANY exit path."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "verdicts.json").write_text(json.dumps(verdicts, indent=2))
    (OUT_DIR / "evidence.json").write_text(json.dumps(_EVIDENCE, indent=2))


if __name__ == "__main__":
    # EVIDENCE MUST SURVIVE A SETUP FAILURE. Every `PROBE SETUP FAILED` path raises SystemExit,
    # and evidence used to be written only after the last question — so the first real run died at
    # field discovery, uploaded nothing ("No files were found with the provided path"), and left
    # the one artifact needed to diagnose it unwritten. A probe whose failures are undiagnosable
    # costs a whole boot cycle per attempt, which on an amd64-only image is the expensive resource.
    try:
        _rc = main()
    except SystemExit as exc:
        _write_evidence({"aborted": str(exc)})
        logger.info(
            "ABORTED — wrote %d recorded round trip(s) to %s/evidence.json before exiting",
            len(_EVIDENCE),
            OUT_DIR,
        )
        raise
    except Exception as exc:  # noqa: BLE001 — an unexpected error is itself evidence worth keeping
        _write_evidence({"crashed": repr(exc)})
        logger.info("CRASHED — wrote %d round trip(s) before re-raising", len(_EVIDENCE))
        raise
    sys.exit(_rc)
