#!/usr/bin/env python3
"""Dependency-advisory gate: lane-aware pip-audit verdicts + advisory escalation.

Bug 63e8-9235-220f-4201. rebar commits its `uv.lock`, so the moment an advisory is
published against a PINNED transitive dependency, a gating `pip-audit` reddens EVERY
change in flight — authored by people who neither caused the advisory nor can fix it.
That is what happened with click 8.2.1 / PYSEC-2026-2132: six changes across five work
streams went red at once and each author independently started diagnosing it.

The fix is not to weaken the scan (it found a real advisory in the shipped environment)
but to route the verdict by LANE:

* **Gerrit verify — "if you touch it, you own it."** A blocking advisory fails the
  Verified gate only when the change under review TOUCHES THE DEPENDENCY MAP (`uv.lock`
  / a `pyproject.toml` dependency declaration / a requirements or constraints file). An
  author already editing the dependency map is in position to resolve the finding and is
  the right owner. A change that leaves the dependency map alone is NEVER blocked by an
  advisory — it is reported as advisory output and the job stays green.
* **Branch / scheduled `main`** — always blocking, so a known-vulnerable pin surfaces
  loudly on the lane whose trigger is "the world changed", not "someone pushed". The
  mirror casts no Gerrit vote, so this red blocks no merge and no submit; what it blocks
  is a RELEASE (below), and it escalates to a ticket via `advisory-alert`.
* **Release** — always blocking. A release never ships on a known-vulnerable pin.

Severity bar (the prevailing OSS convention): **CRITICAL and HIGH fail; MEDIUM warns;
LOW is tracked.** pip-audit itself carries NO severity — its `VulnerabilityResult` is
(id, description, fix_versions, aliases, published) — so severity is enriched from OSV
(`database_specific.severity`) through the injected runner. **An advisory with no
severity attached is treated as HIGH, i.e. it FAILS.** That fallback is deliberate and
is the documented behaviour (docs/dependency-advisory-runbook.md): an unrated advisory
must never be silently ignored, and because OSV enrichment is fail-soft, an enrichment
outage makes the gate STRICTER, never weaker.

Remediation — including when `override-dependencies` is the correct instrument, and why
`constraint-dependencies` cannot express it — is in the runbook, which every failure
message here links to.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# Same sibling-import repair as `canary_bridge.py` (bug 291e-7b48-3f24-41c6): `alert_dedup`
# lives next to this file, so the bare import resolves only when `scripts/` already leads
# sys.path. Derive the directory from `__file__` so it holds under every invocation style;
# the membership check keeps it idempotent.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import alert_dedup  # noqa: E402  (needs _SCRIPTS_DIR on sys.path, set just above)

# (argv) -> (returncode, stdout, stderr) — the seam unit tests replace.
Runner = Callable[[list[str]], tuple[int, str, str]]

RUNBOOK = "docs/dependency-advisory-runbook.md"
RUNBOOK_URL = "https://github.com/navapbc/rebar/blob/main/docs/dependency-advisory-runbook.md"

# ── Severity bar ────────────────────────────────────────────────────────────────────
FAIL_SEVERITIES = frozenset({"CRITICAL", "HIGH"})
WARN_SEVERITIES = frozenset({"MEDIUM", "MODERATE"})
TRACK_SEVERITIES = frozenset({"LOW"})
#: Documented fallback for an advisory carrying no severity: treat it as HIGH (fail), so
#: an unrated advisory is never silently ignored.
UNRATED_FALLBACK = "HIGH"

# ── The dependency map ──────────────────────────────────────────────────────────────
#: Exact repo-relative paths whose edit means "this change touches the dependency map".
DEPENDENCY_MAP_FILES = frozenset({"uv.lock", "pyproject.toml"})
#: Basenames matched anywhere in the tree (workspace members, per-extra requirement sets).
DEPENDENCY_MAP_BASENAMES = frozenset({"uv.lock", "pyproject.toml", "poetry.lock"})
#: Filename prefixes (with a .txt suffix) that declare pins: requirements*.txt,
#: constraints*.txt.
DEPENDENCY_MAP_PREFIXES = ("requirements", "constraints")

LANES = ("gerrit", "branch", "release")

_DB_UNREACHABLE_MARKERS = (
    "temporarily",
    "timed out",
    "timeout",
    "connection",
    "resolve",
    "network",
    "503",
    "502",
    "504",
)


@dataclass(frozen=True)
class Finding:
    """One advisory against one installed distribution."""

    id: str
    package: str
    version: str
    fix_versions: tuple[str, ...]
    severity: str
    severity_source: str  # "osv" | "unrated-fallback"

    @property
    def disposition(self) -> str:
        return classify(self.severity)

    def render(self) -> str:
        fixes = ", ".join(self.fix_versions) if self.fix_versions else "no fix version published"
        rated = "" if self.severity_source == "osv" else " (UNRATED — treated as HIGH)"
        return (
            f"{self.disposition.upper():5s} {self.severity}{rated}"
            f"  {self.id}  {self.package} {self.version}  fix: {fixes}"
        )


def classify(severity: str) -> str:
    """Map a severity to a disposition: ``fail`` / ``warn`` / ``track``."""
    normalized = (severity or "").strip().upper()
    if normalized in WARN_SEVERITIES:
        return "warn"
    if normalized in TRACK_SEVERITIES:
        return "track"
    # CRITICAL, HIGH, and anything unrecognised (incl. "") land on fail — the
    # documented unrated-is-HIGH fallback.
    return "fail"


# ── The "touches the dependency map" signal ────────────────────────────────────────


def touches_dependency_map(paths: Iterable[str]) -> bool:
    """True when any changed path is part of the dependency map.

    "If you touch it, you own it": the signal is the dependency MAP, deliberately not a
    package-level comparison against the advisory's own package. An author editing
    `uv.lock` or a `pyproject` dependency declaration is already resolving dependencies
    and is the right owner for whatever the resulting closure contains — including an
    advisory their edit did not introduce. A finer per-package match would let a
    dependency-map change land while the closure it produced was still vulnerable.
    """
    for raw in paths:
        path = raw.strip().lstrip("./")
        if not path:
            continue
        if path in DEPENDENCY_MAP_FILES:
            return True
        basename = path.rsplit("/", 1)[-1]
        if basename in DEPENDENCY_MAP_BASENAMES:
            return True
        if basename.endswith(".txt") and basename.startswith(DEPENDENCY_MAP_PREFIXES):
            return True
    return False


def changed_files(runner: Runner, rev_range: str) -> tuple[list[str], str]:
    """Return ``(paths, error)`` for ``git diff --name-only <rev_range>``.

    Read-only: `git diff` mutates nothing. ``error`` is non-empty when the range could
    not be resolved (e.g. a checkout too shallow to see the parent) — callers FAIL
    CLOSED on that, treating the change as dependency-map-touching, because an unknown
    diff must never be the reason an advisory goes unblocked.
    """
    rc, stdout, stderr = runner(["git", "diff", "--name-only", rev_range])
    if rc != 0:
        return [], (stderr.strip() or f"git diff {rev_range} failed (exit {rc})")
    return [line for line in stdout.splitlines() if line.strip()], ""


# ── pip-audit invocation + parsing ─────────────────────────────────────────────────


def is_db_unreachable(text: str) -> bool:
    """True when pip-audit's failure output looks like an advisory-DB reachability error.

    This is the ONLY recheckable failure mode. A real finding is not cleared by re-running
    — which matters because `recheck` is every author's first instinct and the single most
    time-wasting property recorded on bug 63e8.
    """
    lowered = text.lower()
    return any(marker in lowered for marker in _DB_UNREACHABLE_MARKERS)


def _retry_sleep(seconds: float) -> None:
    """The default retry sleeper: a patchable indirection over ``time.sleep``.

    Deliberately a named function rather than ``sleeper=time.sleep`` in the
    signature. A default expression is evaluated ONCE at import, capturing whatever
    ``time.sleep`` was then, so a test patching ``time.sleep`` afterwards could not
    reach it and really slept the 5s + 10s backoff (ticket 5ea3-76e5-480a-4464).
    Resolving ``time.sleep`` at CALL time keeps the seam patchable while leaving
    production behaviour byte-identical.
    """
    time.sleep(seconds)


def run_pip_audit(
    runner: Runner,
    *,
    attempts: int = 3,
    sleeper: Callable[[float], None] = _retry_sleep,
    extra_args: Sequence[str] = (),
) -> tuple[str, str]:
    """Run pip-audit with JSON output, retrying ONLY DB-unreachable failures.

    Returns ``(stdout_json, error)``. ``error`` is non-empty only for an
    infrastructure failure (DB unreachable after every attempt, or unusable output);
    a real finding is a SUCCESSFUL audit whose JSON carries vulns, not an error.
    """
    argv = ["pip-audit", "--format", "json", "--progress-spinner", "off", *extra_args]
    last = ""
    for attempt in range(1, attempts + 1):
        rc, stdout, stderr = runner(argv)
        # pip-audit exits 1 when it FINDS something; the JSON on stdout is still the
        # answer. Only an empty/unparseable stdout is an actual failure.
        if stdout.strip():
            return stdout, ""
        last = (stderr or stdout).strip()
        if not is_db_unreachable(last):
            return "", f"pip-audit produced no JSON (exit {rc}): {last}"
        print(f"pip-audit attempt {attempt}/{attempts}: transient DB error — backing off.")
        if attempt < attempts:
            sleeper(attempt * 5)
    return "", f"advisory DB unreachable after {attempts} attempts: {last}"


def parse_pip_audit_json(text: str) -> list[dict[str, object]]:
    """Flatten pip-audit's JSON into ``{id, package, version, fix_versions}`` dicts."""
    data = json.loads(text)
    dependencies = data.get("dependencies") if isinstance(data, dict) else data
    out: list[dict[str, object]] = []
    for dep in dependencies or []:
        if not isinstance(dep, dict):
            continue
        for vuln in dep.get("vulns") or []:
            if not isinstance(vuln, dict):
                continue
            out.append(
                {
                    "id": str(vuln.get("id", "")),
                    "package": str(dep.get("name", "")),
                    "version": str(dep.get("version", "")),
                    "fix_versions": tuple(str(v) for v in (vuln.get("fix_versions") or [])),
                }
            )
    return out


def osv_severity(runner: Runner, vuln_id: str) -> str:
    """Best-effort severity for ``vuln_id`` from OSV, or ``''`` when unavailable.

    Fail-soft by construction: every error path returns ``''``, which classify() turns
    into the unrated-is-HIGH fallback. An OSV outage therefore makes the gate stricter.
    """
    if not vuln_id:
        return ""
    rc, stdout, _stderr = runner(["curl", "-fsS", f"https://api.osv.dev/v1/vulns/{vuln_id}"])
    if rc != 0 or not stdout.strip():
        return ""
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    specific = payload.get("database_specific")
    if isinstance(specific, dict):
        severity = specific.get("severity")
        if isinstance(severity, str) and severity.strip():
            return severity.strip().upper()
    return ""


def collect_findings(runner: Runner, audit_json: str, *, enrich: bool = True) -> list[Finding]:
    """Parse pip-audit JSON and attach a severity to every advisory."""
    findings: list[Finding] = []
    for raw in parse_pip_audit_json(audit_json):
        vuln_id = str(raw["id"])
        severity = osv_severity(runner, vuln_id) if enrich else ""
        findings.append(
            Finding(
                id=vuln_id,
                package=str(raw["package"]),
                version=str(raw["version"]),
                fix_versions=tuple(raw["fix_versions"]),  # type: ignore[arg-type]
                severity=severity or UNRATED_FALLBACK,
                severity_source="osv" if severity else "unrated-fallback",
            )
        )
    return findings


# ── The lane verdict ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Verdict:
    blocking: bool
    reason: str
    fail_ids: tuple[str, ...]
    warn_ids: tuple[str, ...]
    track_ids: tuple[str, ...]


def decide(findings: Sequence[Finding], *, lane: str, touches_map: bool) -> Verdict:
    """Lane-aware verdict over classified findings.

    The whole of bug 63e8's fix is this function: ``gerrit`` + ``touches_map=False`` is
    the ONLY combination in which a fail-severity advisory does not block.
    """
    if lane not in LANES:
        raise ValueError(f"unknown lane {lane!r} (expected one of {', '.join(LANES)})")
    fail_ids = tuple(f.id for f in findings if f.disposition == "fail")
    warn_ids = tuple(f.id for f in findings if f.disposition == "warn")
    track_ids = tuple(f.id for f in findings if f.disposition == "track")

    if not fail_ids:
        return Verdict(False, "no CRITICAL/HIGH advisories", fail_ids, warn_ids, track_ids)
    if lane == "gerrit" and not touches_map:
        return Verdict(
            False,
            "advisory present but this change does not touch the dependency map — "
            "not this author's to own (bug 63e8); the scheduled main lane owns it",
            fail_ids,
            warn_ids,
            track_ids,
        )
    if lane == "gerrit":
        return Verdict(
            True,
            "this change touches the dependency map, so it owns the advisories in the "
            "closure it produces",
            fail_ids,
            warn_ids,
            track_ids,
        )
    if lane == "release":
        reason = "a release never ships on a known-vulnerable pin"
    else:
        reason = "main must surface a known-vulnerable pin loudly"
    return Verdict(True, reason, fail_ids, warn_ids, track_ids)


def render_report(findings: Sequence[Finding], verdict: Verdict, *, lane: str) -> str:
    lines = [f"== dependency advisories (lane: {lane}) =="]
    if not findings:
        lines.append("pip-audit: OK (no known vulnerabilities).")
        return "\n".join(lines)
    lines.extend(f.render() for f in sorted(findings, key=lambda f: (f.package, f.id)))
    lines.append("")
    lines.append(
        f"verdict: {'BLOCKING' if verdict.blocking else 'advisory-only'} — {verdict.reason}"
    )
    lines.append(f"runbook: {RUNBOOK} ({RUNBOOK_URL})")
    lines.append(
        "NOTE: `recheck` cannot clear a real finding — only an advisory-DB reachability "
        "failure is retryable."
    )
    return "\n".join(lines)


def _append_outputs(path: str, **kv: str) -> None:
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in kv.items():
            handle.write(f"{key}={value}\n")


# ── Subcommand: gate ───────────────────────────────────────────────────────────────


def cmd_gate(
    args: argparse.Namespace,
    runner: Runner,
    environ: Mapping[str, str],
    now_epoch: int,
) -> int:
    lane = args.lane
    touches_map = True
    if lane == "gerrit":
        if args.changed_files is not None:
            paths = [p for p in args.changed_files.splitlines() if p.strip()]
            touches_map = touches_dependency_map(paths)
        else:
            paths, err = changed_files(runner, args.rev_range)
            if err:
                print(
                    f"::warning::could not resolve changed files ({err}) — "
                    "failing CLOSED, treating the change as dependency-map-touching."
                )
                touches_map = True
            else:
                touches_map = touches_dependency_map(paths)
        print(f"dependency map touched by this change: {str(touches_map).lower()}")

    audit_json, err = run_pip_audit(runner, attempts=args.attempts)
    if err:
        print(
            f"::error::pip-audit could not complete — INFRASTRUCTURE issue, not a "
            f"vulnerability: {err}. Re-run the job / comment 'recheck'. See {RUNBOOK}.",
            file=sys.stderr,
        )
        return 1

    try:
        findings = collect_findings(runner, audit_json, enrich=not args.no_enrich)
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"::error::pip-audit JSON unparseable: {exc}", file=sys.stderr)
        return 1

    verdict = decide(findings, lane=lane, touches_map=touches_map)
    report = render_report(findings, verdict, lane=lane)
    print(report)

    gh_output = environ.get("GITHUB_OUTPUT")
    if gh_output:
        _append_outputs(
            gh_output,
            blocking="true" if verdict.blocking else "false",
            fail_count=str(len(verdict.fail_ids)),
            advisory_ids=",".join(verdict.fail_ids),
            summary=(
                "; ".join(f"{f.id} ({f.package} {f.version}, {f.severity})" for f in findings)
                or "none"
            ),
        )

    if verdict.warn_ids:
        print(f"::warning::MEDIUM advisories present (non-blocking): {', '.join(verdict.warn_ids)}")
    if verdict.track_ids:
        print(f"LOW advisories tracked (non-blocking): {', '.join(verdict.track_ids)}")

    if verdict.blocking:
        print(
            f"::error::dependency advisories block this lane ({lane}): "
            f"{', '.join(verdict.fail_ids)} — {verdict.reason}. Remediation: {RUNBOOK_URL}",
            file=sys.stderr,
        )
        return 1
    if verdict.fail_ids:
        print(
            f"::warning::dependency advisories present but NOT blocking this lane: "
            f"{', '.join(verdict.fail_ids)} — {verdict.reason}. See {RUNBOOK}."
        )
    return 0


# ── Subcommand: advisory-alert (scheduled-lane escalation) ─────────────────────────

ADVISORY_MARKER = "DEPENDENCY_ADVISORY_ALERT:"


def _advisory_description(summary: str, ids: str, ts: str, run_url: str) -> str:
    return (
        "# Outstanding dependency advisory on `main`\n\n"
        "The scheduled dependency-advisory lane found CRITICAL/HIGH advisories against the "
        "committed lock. Per bug `63e8`, this does NOT block merges or Gerrit submit for "
        "changes that leave the dependency map alone — it blocks **releases**, and it is "
        "owned here rather than by whoever happens to have a change in flight.\n\n"
        f"- **Advisories:** {ids or 'none'}\n"
        f"- **Detail:** {summary or 'n/a'}\n"
        f"- **Detected at:** {ts}\n"
        f"- **Run:** {run_url}\n\n"
        f"## Remediation\n\nFollow `{RUNBOOK}`: upgrade the direct dependency first; if an "
        "upstream cap blocks that, `override-dependencies` with a recorded justification; "
        "then track the upstream fix so the override can be removed. `constraint-dependencies` "
        "cannot express this — it only NARROWS, and returns UNSATISFIABLE as its expected "
        "output.\n\nThis ticket auto-closes when the advisory clears.\n"
    )


def cmd_advisory_alert(
    args: argparse.Namespace,
    runner: Runner,
    environ: Mapping[str, str],
    now_epoch: int,
) -> int:
    if environ.get("DRY_RUN") == "true":
        print("DRY_RUN — not filing/updating/closing the advisory ticket.")
        return 0

    tag = environ.get("ADVISORY_TAG", "dependency-advisory-alert")
    blocking = environ.get("BLOCKING", "false") == "true"
    ids = environ.get("ADVISORY_IDS", "")
    summary = environ.get("ADVISORY_SUMMARY", "")
    run_url = environ.get("RUN_URL", "")
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_epoch))

    if blocking and not ids.strip():
        print(
            "::error::advisory-alert invoked blocking with no advisory ids — refusing to "
            "file a hollow ticket. Fix the gate wiring."
        )
        return 1

    # DEDUP: one open bug per tag, found before anything is filed. A daily lane against an
    # advisory that stays open for weeks updates THIS ticket; it never files a second.
    tid = alert_dedup.find_alert_ticket(runner, tag)

    if blocking and not tid:
        title = f"[dependency-advisory] outstanding advisory on main: {ids}"
        rc, _out, stderr = runner(
            [
                "rebar",
                "create",
                "bug",
                title,
                "--priority",
                "1",
                "--tags",
                tag,
                "--description",
                _advisory_description(summary, ids, ts, run_url),
                "--detected-by",
                "dependency-advisory-canary",
            ]
        )
        if rc != 0:
            print(stderr)
            return rc
        print(f"Filed a new dependency-advisory ticket for: {ids}")
    elif blocking and tid:
        # Accumulation cap: at most one marker comment per 24h on the existing ticket.
        if alert_dedup.recent_marker_comment(runner, tid, ADVISORY_MARKER, now_epoch):
            print(f"Alert ticket {tid} already has a marker comment <24h old — skipping.")
            return 0
        body = f"{ADVISORY_MARKER} Still outstanding as of {ts}: {ids}. {summary} Run: {run_url}"
        rc, _out, stderr = runner(["rebar", "comment", tid, body])
        if rc != 0:
            print(stderr)
            return rc
    elif not blocking and tid:
        reason = f"Fixed: dependency advisories cleared on main at {ts}."
        force_close = (
            f"Fixed: dependency advisories cleared at {ts} (bot alert auto-close; advisory"
            " tickets have no completion criteria to verify)."
        )
        rc, _out, stderr = runner(
            [
                "rebar",
                "transition",
                tid,
                "open",
                "closed",
                "--class",
                "env_integration",
                "--reason",
                reason,
                f"--force={force_close}",
            ]
        )
        if rc != 0:
            print(stderr)
            return rc
        print(f"Closed advisory ticket {tid} — advisories cleared.")
    else:
        print("No advisories and no open advisory ticket — nothing to do.")
    return 0


# ── CLI ────────────────────────────────────────────────────────────────────────────


# raw-git-ok: generic command runner, argv supplied by caller (read-only git diff + curl)
def _default_runner(argv: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    return result.returncode, result.stdout, result.stderr


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    gate = sub.add_parser("gate", help="lane-aware pip-audit verdict")
    gate.add_argument("--lane", choices=LANES, required=True)
    gate.add_argument(
        "--rev-range",
        default="HEAD^..HEAD",
        help="git range whose changed files decide dependency-map ownership (gerrit lane)",
    )
    gate.add_argument(
        "--changed-files",
        default=None,
        help="newline-separated paths, bypassing git (testing / precomputed lanes)",
    )
    gate.add_argument("--attempts", type=int, default=3)
    gate.add_argument(
        "--no-enrich",
        action="store_true",
        help="skip OSV severity enrichment (every finding falls back to HIGH)",
    )

    sub.add_parser("advisory-alert", help="file/update/close the advisory ticket")
    return parser


_COMMANDS = {"gate": cmd_gate, "advisory-alert": cmd_advisory_alert}


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner = _default_runner,
    environ: Mapping[str, str] | None = None,
    now_epoch: int | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    return _COMMANDS[args.command](
        args,
        runner,
        os.environ if environ is None else environ,
        int(time.time()) if now_epoch is None else now_epoch,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
