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

# The readiness definition is SHARED with the live harness fixture
# (`tests/external/live_jira_dc/conftest.py`) — bug 9790-cafa-dffa-462e. It lives next
# to this file, which is importable as-is under `python scripts/<this file>` because
# the script's directory leads sys.path; the insert below is defensive so the probe
# also imports cleanly when invoked from elsewhere (e.g. `python -m`, or a runner that
# rewrites sys.path[0]).
_SCRIPTS_DIR = str(pathlib.Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import jira_dc_field_readiness  # noqa: E402

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


def _create_issue(project: str, issuetype: str, summary: str, extra: dict | None = None) -> str:
    fields = {"project": {"key": project}, "issuetype": {"name": issuetype}, "summary": summary}
    fields.update(extra or {})
    status, body = _req("/rest/api/2/issue", method="POST", payload={"fields": fields})
    if status != 201 or not isinstance(body, dict):
        raise SystemExit(f"PROBE SETUP FAILED: could not create {issuetype}: {status} {body!r}")
    return str(body["key"])


def _await_named_fields(names: tuple[str, ...]) -> dict[str, str | None]:
    """Wait for the Jira Software custom fields to REGISTER, then map name -> field id.

    **CALL THIS ONLY AFTER THE PROBE'S PROJECT HAS BEEN CREATED** (bug
    941b-f049-5f29-4410). GreenHopper registers ``Epic Link``/``Epic Name`` on the
    first Jira Software project create, so calling it earlier waits for something only
    the blocked step can produce — which is exactly how probe runs 30944211742 and
    30930839323 both died at ``PROBE SETUP FAILED: Epic Link=None Epic Name=None``.
    ``main`` below now creates the project first and calls this immediately after.

    Delegates the whole definition to ``jira_dc_field_readiness`` so this probe and the
    live harness fixture cannot drift apart again (bug 9790-cafa-dffa-462e): change
    1387 fixed this path alone, which left two answers to one question in the tree.

    Returns the same ``dict[str, str | None]`` shape ``main`` expects; values may be
    ``None`` on expiry so the caller can report exactly which name never arrived.
    """
    result = jira_dc_field_readiness.await_required_fields(
        # Resolved at call time from the module globals, not captured at import, so a
        # patched ``_req`` (and its evidence recording) is honoured.
        lambda path: _req(path),
        names=names,
    )
    # Log the SHARED success prose on the happy path (bug 941b-f049-5f29-4410): the wait
    # used to record nothing when it worked, so no probe run ever reported how long
    # provisioning actually took and the only numbers anyone had came from expiries. The
    # failure path keeps dumping the OBSERVED inventory, which is what separates "this ran
    # before a Software project existed" from "the image genuinely lacks these fields".
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

    # ── setup ────────────────────────────────────────────────────────────────────────────────
    # PROJECT FIRST, FIELDS SECOND — and that ORDER is the whole of bug
    # 941b-f049-5f29-4410's fix on this side. GreenHopper registers `Epic Link`/`Epic Name`
    # when the first Jira Software project is created, NOT when the plugin starts, so the
    # previous order (wait for the fields, then create the project) waited on a thing only
    # the step it was blocking could produce. Both real probe runs died there —
    # 30944211742 and 30930839323, each with `PROBE SETUP FAILED: Epic Link=None Epic
    # Name=None`. Experiment run 30981084637 measured the mechanism directly: 27 fields and
    # zero customfield_* entries before the create AND after 180s of quiet, then 55 fields
    # with both Epic fields present 0.0512s after the create.
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

    # NOW the fields can exist — resolve their instance ids, which every Epic Link
    # write/clear check below is written in terms of. Still a bounded WAIT rather than one
    # read: 0.0512s is fast but not atomic with the 201 above, so a single read can lose
    # that race. `REQUIRED_FIELDS` is the SHARED tuple, not a local literal — the harness
    # asserts the same names, and two independent literals is precisely the drift bug 9790
    # collapsed.
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
