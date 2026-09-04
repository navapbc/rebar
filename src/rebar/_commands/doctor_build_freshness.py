"""Build-freshness diagnostics for ``rebar doctor`` — is this box's rebar current?

Answers one operator question nothing else on the box could answer without a remote
service: *is the ``rebar`` I am about to run stale, and is whatever keeps it current
still working?*

**Why this exists.** A host may keep its global ``rebar``/``rebar-mcp`` aligned to
``origin/main`` with a scheduled updater that builds each candidate commit in a release
directory and publishes it through an atomic ``current`` pointer. On the host that
motivated this module (bug ae97-a37b-9fa3-413a) that updater rejected **120 consecutive**
candidates on a uv ``required-version`` mismatch and left the global build ~195 commits
and five days behind ``origin/main``. Every agent session on the box ran gates from it,
including plan-review and completion-verifier verdicts, whose own advisory says results
"can be wrong in ways the verdict cannot show". The one detector that existed was a remote
alert sink, and it failed to deliver on every hourly attempt (a cross-account IAM 403), so
a streak of 120 accumulated against a threshold of 3 with **zero** operator signal. That
was already the second occurrence of the shape: the same alert had been added after an
earlier stall of 43 rejections / ~48 commits.

The lesson is the design constraint: **a detector that can only speak through a remote
sink is a detector that can go silent without saying so.** Everything here is read from
local disk, with no network, no credential, and no CI provider — it answers identically on
a laptop and in a pipeline (``project.portability``).

**Two independent signals, deliberately.** Either alone has a blind spot, and the incident
needed both to be understood:

* :data:`KIND_REJECT_STREAK` — the updater's own consecutive-rejection counter
  (``<state>/reject-streak``). It says the updater is *failing*, but not how far behind
  that has left the build.
* :data:`KIND_BUILD_STALE` — the distance from the build the updater actually PUBLISHED
  (resolved through its ``current`` pointer) to ``origin/main``. It says how much staleness
  has accumulated, and it fires even when the counter is missing, reset by hand, or the
  updater stopped running altogether rather than failing.

**Reuse, not re-derivation.** The commit-distance walk is
:func:`rebar.llm.build_drift.detect_drift` — the same ancestry logic that already warns a
gate about its own binary — invoked with the published build's sha. A second walk here
would be a fork that drifts from the one gates obey.

**Absent state is NOT a fault.** Most developers run no such updater, and a check that
reports a healthy box as broken is a check that gets ignored — which is how a real 120-run
stall stays invisible. A missing state directory yields exactly one
:data:`SEVERITY_UNAVAILABLE` finding, and :func:`has_blocking_build_freshness` is false.

**Read-only and advisory.** Nothing here writes, kicks the updater, or reclaims a release
directory, and ``doctor --repair`` cannot reach these findings (they live in their own
list, outside the repair loop). They are also outside ``doctor``'s exit code, for the
reason :mod:`rebar._commands.doctor_mcp_client` states about client configs: they describe
the HOST, not the store, so gating store health on them would make the exit depend on
whichever scheduled agents happen to run on the box. :func:`has_blocking_build_freshness`
is the seam for a caller that does want to gate on them.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Contract with the updater's on-disk state
# ---------------------------------------------------------------------------

#: Directory name under the XDG state root that the main-tracking updater owns.
STATE_DIR_NAME = "rebar-dev"

#: The path segments under ``$HOME`` that hold it. This mirrors the updater's OWN
#: default (``${HOME}/.local/state/rebar-dev``) rather than generalising over
#: ``$XDG_STATE_HOME``: the tool being observed does not consult that variable, so
#: honouring it here would describe a layout the updater never writes.
_STATE_ROOT_SEGMENTS = (".local", "state")

#: The updater's consecutive-rejection counter: a single decimal integer, rewritten on
#: every run (reset to 0 on a successful publish).
REJECT_STREAK_FILE = "reject-streak"

#: The atomically-swapped pointer to the live release directory. The published commit is
#: in its ``sha`` file; the symlink target (``releases/<sha>.<pid>``) is the fallback for
#: a release published before that file existed.
CURRENT_LINK = "current"
CURRENT_SHA_FILE = "sha"

#: The ref the updater tracks. Resolved in the local repo, so this needs no network — a
#: checkout that has not fetched recently simply measures against the ref it has, which
#: understates drift rather than inventing it.
TRACKED_REF = "origin/main"

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

#: Consecutive rejections before the streak is a finding. Mirrors the updater's own
#: alert threshold, so the local detector and the remote alert agree on when a run of
#: failures stops being noise — and this one still speaks when the remote sink cannot.
DEFAULT_REJECT_STREAK_ALERT = 3

#: Commits behind ``origin/main`` before the published build is a finding. An hourly
#: updater is routinely zero to a couple of commits behind, so the healthy population sits
#: near zero. Both recorded incidents were far above this line — ~48 commits at the first,
#: ~195 at the second — and 25 sits below the smaller of them while staying an order of
#: magnitude above healthy, so it catches a recurrence of either without firing on a box
#: that is merely between hourly runs.
DEFAULT_MAX_COMMITS_BEHIND = 25

# ---------------------------------------------------------------------------
# Finding vocabulary
# ---------------------------------------------------------------------------

KIND_UPDATER_ABSENT = "updater-absent"
KIND_REJECT_STREAK = "reject-streak"
KIND_BUILD_STALE = "build-stale"
KIND_STATE_UNREADABLE = "state-unreadable"
KIND_OK = "ok"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_UNAVAILABLE = "unavailable"
SEVERITY_OK = "ok"

_SECTION_HEADER = "doctor: build freshness"


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


def default_state_dir(*, home: Path | None = None) -> Path:
    """The updater's state directory by convention, without probing for it.

    ``home`` is injectable so the resolution rule is testable without touching the real
    home directory. An operator who has relocated the state directory (the updater takes
    its own override) passes ``state_dir`` to :func:`scan_build_freshness` instead — this
    module reads no environment of its own, so it adds no configuration surface.
    """
    base = Path.home() if home is None else Path(home)
    return base.joinpath(*_STATE_ROOT_SEGMENTS) / STATE_DIR_NAME


def scan_build_freshness(
    *,
    state_dir: Path | None = None,
    repo_root: str | None = None,
    reject_streak_alert: int = DEFAULT_REJECT_STREAK_ALERT,
    max_commits_behind: int = DEFAULT_MAX_COMMITS_BEHIND,
) -> list[dict[str, Any]]:
    """Report on the local main-tracking updater and the build it published.

    Returns a flat list of finding dicts, each carrying at least ``kind``, ``severity``
    and ``detail``. Never raises: every degradation — no state directory, an unreadable
    counter, a published sha git cannot resolve, no git at all — becomes a finding or
    silence, because a diagnostic that can crash is one more thing that goes quiet.
    """
    state = default_state_dir() if state_dir is None else Path(state_dir)
    if not state.is_dir():
        return [
            _finding(
                KIND_UPDATER_ABSENT,
                SEVERITY_UNAVAILABLE,
                f"no main-tracking updater state at {state} — not applicable on this box",
                state_dir=str(state),
            )
        ]

    findings = _scan_reject_streak(state, reject_streak_alert)
    findings.extend(_scan_published_build(state, repo_root, max_commits_behind))
    if not any(f["severity"] in (SEVERITY_ERROR, SEVERITY_WARNING) for f in findings):
        findings.append(
            _finding(
                KIND_OK,
                SEVERITY_OK,
                f"updater state at {state} looks healthy",
                state_dir=str(state),
            )
        )
    return findings


def has_blocking_build_freshness(findings: Iterable[Mapping[str, Any]]) -> bool:
    """True when any finding is an error.

    ``doctor`` reports these but does NOT fold them into its exit code — they describe
    the host's scheduled updater, not the store. This predicate is the seam for a caller
    that does want to gate on build freshness.
    """
    return any(f.get("severity") == SEVERITY_ERROR for f in findings)


def render_text(findings: Iterable[Mapping[str, Any]]) -> list[str]:
    """Render the section as text lines (the caller prints them).

    A header is always emitted, healthy boxes included: "the updater is fine" is the
    answer an operator most often needs, and omitting it leaves them unable to tell a
    healthy box from a check that never ran — the exact ambiguity that let a 120-run
    stall pass for normal.
    """
    lines = [_SECTION_HEADER]
    lines.extend(
        f"  [{f.get('severity', '?')}] {f.get('kind', '?')}: {f.get('detail', '')}"
        for f in findings
    )
    return lines


# ---------------------------------------------------------------------------
# Finding builder
# ---------------------------------------------------------------------------


def _finding(kind: str, severity: str, detail: str, **extra: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"kind": kind, "severity": severity, "detail": detail}
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# Signal 1 — the updater's consecutive-rejection counter
# ---------------------------------------------------------------------------


def _scan_reject_streak(state: Path, threshold: int) -> list[dict[str, Any]]:
    path = state / REJECT_STREAK_FILE
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        # No counter is not evidence of a stall: a box whose updater has simply never
        # rejected anything may never have written the file. The published-build check
        # below still measures staleness independently.
        return []
    try:
        streak = int(raw)
    except ValueError:
        return [
            _finding(
                KIND_STATE_UNREADABLE,
                SEVERITY_WARNING,
                f"{path} does not hold an integer ({raw!r}) — the rejection streak "
                "cannot be read, so a stalled updater would go undetected here",
                path=str(path),
            )
        ]
    if streak < threshold:
        return []
    return [
        _finding(
            KIND_REJECT_STREAK,
            SEVERITY_ERROR,
            f"the main-tracking updater has rejected {streak} consecutive candidates "
            f"(threshold {threshold}); the live build is pinned and drifting from "
            f"{TRACKED_REF} — inspect {state / 'update.log'}",
            streak=streak,
            threshold=threshold,
            path=str(path),
        )
    ]


# ---------------------------------------------------------------------------
# Signal 2 — how far the PUBLISHED build is behind the tracked ref
# ---------------------------------------------------------------------------


def published_build_sha(state: Path) -> str | None:
    """The commit of the build behind the updater's ``current`` pointer, or ``None``.

    Prefers the ``sha`` file the updater writes into each release; falls back to the
    ``releases/<sha>.<pid>`` naming of the pointer's own target, which is the only
    provenance a release published before that file existed carries.
    """
    current = state / CURRENT_LINK
    try:
        text = (current / CURRENT_SHA_FILE).read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        text = ""
    if not text:
        try:
            text = os.readlink(current).rsplit("/", 1)[-1].split(".", 1)[0]
        except OSError:
            return None
    return text or None


def _scan_published_build(
    state: Path, repo_root: str | None, max_commits_behind: int
) -> list[dict[str, Any]]:
    sha = published_build_sha(state)
    if not sha:
        return []
    # Reuses the gate-side ancestry walk rather than forking a second one. It answers
    # None for every "cannot prove drift" case — a sha absent from this repo, an
    # unresolvable ref, git unavailable — and silence is the right answer to all of them:
    # an unproven claim of staleness would teach operators to ignore this section.
    from rebar.llm import build_drift

    try:
        drift = build_drift.detect_drift(TRACKED_REF, repo_root, build_sha=sha)
    # detect_drift already degrades every git fault to None internally; this guards the
    # residue it does not (a bad path, an unparseable count). A diagnostic must never be
    # the thing that fails the command it is reporting inside.
    except (OSError, ValueError):
        return []
    if drift is None or drift.commits_behind <= max_commits_behind:
        return []
    return [
        _finding(
            KIND_BUILD_STALE,
            SEVERITY_ERROR,
            f"the published rebar build ({drift.short_build_sha}, "
            f"{drift.build_date or 'date unknown'}) is {drift.commits_behind} commit(s) "
            f"behind {TRACKED_REF} ({drift.short_pinned_sha}), over the {max_commits_behind}"
            "-commit threshold — gate results from it can be wrong in ways the verdict "
            "cannot show",
            build_sha=drift.build_sha,
            build_date=drift.build_date,
            tracked_sha=drift.pinned_sha,
            commits_behind=drift.commits_behind,
            threshold=max_commits_behind,
        )
    ]
