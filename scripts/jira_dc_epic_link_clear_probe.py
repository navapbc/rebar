"""Probe Epic Link clearing and `parent` screenability on Jira DC 8.17.1.

Q1 tries the fields form against an established Epic Link, then tries the update form
only if needed. Q2 checks whether `parent` appears in the screenable field inventory.
The predetermined requests use bounded ``urllib`` calls and do not rerun or supersede
the broader capability map.

The dedicated workflow supplies the digest-pinned linux/amd64 harness described in
``tests/external/live_jira_dc/README.md``. The probe writes every request and response to
JSON and logs one verdict for each question. It does not modify the repository.
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

# Share the readiness definition with ``tests/external/live_jira_dc/conftest.py``. Add
# this directory explicitly so imports also work when a runner rewrites ``sys.path[0]``.
_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import jira_dc_field_readiness  # noqa: E402

BASE = os.environ.get("JIRA_DC_BASE_URL", "http://localhost:2990/jira").rstrip("/")
USER = os.environ.get("JIRA_DC_USER", "admin")
PASSWORD = os.environ.get("JIRA_DC_PASSWORD", "admin")
OUT_DIR = pathlib.Path(os.environ.get("JIRA_DC_PROBE_OUTPUT_DIR", "jira-dc-epic-link-probe"))

# Use logging because ``scripts/`` is outside the closed T201 print allowlist.
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("jira-dc-epic-link-probe")

# Match the project template pinned by ``tests/external/live_jira_dc/conftest.py``.
PROJECT_TEMPLATE = "com.pyxis.greenhopper.jira:gh-scrum-template"

_EVIDENCE: list[dict] = []


def _req(path: str, *, method: str = "GET", payload: dict | None = None) -> tuple[int, object]:
    """One raw REST round trip, recorded verbatim into the evidence log."""
    url = f"{BASE}{path}"
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)  # fixed localhost harness
    token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    request.add_header("Authorization", f"Basic {token}")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
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


def _create_issue(project: str, issuetype: str, summary: str, extra: dict | None = None) -> str:
    fields = {"project": {"key": project}, "issuetype": {"name": issuetype}, "summary": summary}
    fields.update(extra or {})
    status, body = _req("/rest/api/2/issue", method="POST", payload={"fields": fields})
    if status != 201 or not isinstance(body, dict):
        raise SystemExit(f"PROBE SETUP FAILED: could not create {issuetype}: {status} {body!r}")
    return str(body["key"])


def _await_named_fields(names: tuple[str, ...]) -> dict[str, str | None]:
    """Wait after project creation and map each requested field name to its ID.

    GreenHopper registers these fields when the first Jira Software project is created.
    The shared readiness helper keeps this probe aligned with the external-test fixture.
    Values remain ``None`` when the bounded wait expires.
    """
    result = jira_dc_field_readiness.await_required_fields(
        # Resolve ``_req`` at call time so patched evidence recording remains effective.
        lambda path: _req(path),
        names=names,
    )
    # Record elapsed time on success and the discriminating field inventory on failure.
    if result.ready:
        logger.info("  %s", jira_dc_field_readiness.ready_message(result, base_url=BASE))
    else:
        logger.info(
            "  %s",
            jira_dc_field_readiness.not_ready_message(
                result,
                base_url=BASE,
                budget=jira_dc_field_readiness.FIELD_READY_BUDGET_S,
            ),
        )
    return result.ids


def main() -> int:
    verdicts: dict[str, str] = {}

    # GreenHopper creates Epic fields after the first Jira Software project. Run
    # 30981084637 measured both fields 0.0512 seconds later for ticket
    # 941b-f049-5f29-4410.
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

    # Use the shared names and a bounded poll because field registration is not atomic with
    # the project creation response.
    _fields = _await_named_fields(jira_dc_field_readiness.REQUIRED_FIELDS)
    epic_link = _fields["Epic Link"]
    epic_name = _fields["Epic Name"]
    if not epic_link or not epic_name:
        raise SystemExit(
            f"PROBE SETUP FAILED: Epic Link={epic_link!r} Epic Name={epic_name!r} — "
            f"these are Jira Software (GreenHopper) custom fields and they did not appear "
            f"even though project {key!r} was created first. Read the inventory dumped "
            "above: no customfield_* entries at all would mean the create did not actually "
            "provision (check its HTTP 201 in the evidence log), whereas other custom "
            "fields present without these two means this image genuinely dropped the Epic "
            "fields (bugs 9790-cafa-dffa-462e / 941b-f049-5f29-4410)."
        )

    epic = _create_issue(key, "Epic", "probe epic", {epic_name: "probe epic"})
    child = _create_issue(key, "Task", "probe child")

    # Establish and read back the Epic Link before testing either clearing shape.
    _req(f"/rest/api/2/issue/{child}", method="PUT", payload={"fields": {epic_link: epic}})
    status, body = _req(f"/rest/api/2/issue/{child}?fields={epic_link}")
    landed = (body.get("fields") or {}).get(epic_link) if isinstance(body, dict) else None
    if landed != epic:
        raise SystemExit(f"PROBE SETUP FAILED: Epic Link never reached {child}: {landed!r}")
    verdicts["setup_epic_link_write"] = f"OK — {child} Epic Link = {landed}"

    # ── Q1a: clear via the fields form ───────────────────────────────────────────────────────
    s1, _b1 = _req(
        f"/rest/api/2/issue/{child}", method="PUT", payload={"fields": {epic_link: None}}
    )
    status, body = _req(f"/rest/api/2/issue/{child}?fields={epic_link}")
    after1 = (body.get("fields") or {}).get(epic_link) if isinstance(body, dict) else "<unread>"
    verdicts["q1a_fields_null"] = (
        f"PUT fields.{epic_link}=null -> HTTP {s1}; read-back = {after1!r} — "
        + ("CLEARED" if not after1 else "IGNORED (still set)")
    )

    # ── Q1b: the update-verb form, only if the first did not clear ───────────────────────────
    if after1:
        s2, _b2 = _req(
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
    # Record parent-named fields to distinguish Parent Link from the system ``parent`` field.
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
    """Persist verdicts and every recorded round trip on any exit path."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "verdicts.json").write_text(json.dumps(verdicts, indent=2))
    (OUT_DIR / "evidence.json").write_text(json.dumps(_EVIDENCE, indent=2))


if __name__ == "__main__":
    # Preserve recorded requests when setup exits before either question completes.
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
    except Exception as exc:  # an unexpected error is itself evidence worth keeping
        _write_evidence({"crashed": repr(exc)})
        logger.info("CRASHED — wrote %d round trip(s) before re-raising", len(_EVIDENCE))
        raise
    sys.exit(_rc)
