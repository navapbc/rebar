"""Warn when the rebar build running a gate is older than the ref that gate pins.

Every code-reading gate (``review_plan``, ``verify_completion``, ``review_code``,
``scan_spec``) resolves a base ref to a pinned SHA and materializes a snapshot of the
repo at that SHA (see :mod:`rebar.llm.gate_source`). Nothing, however, compared the
RUNNING BUILD against that SHA — so a checkout that had drifted behind ``origin/main``
would review brand-new material with old gate code, silently.

The failure mode is quiet by construction. In the incident that motivated this module
(ticket b273-e0ba-f719-4f1c) a build predating the commit that renamed
``verify.overlap_enabled`` to ``verify.suggest_duplicate_tickets`` read the CURRENT base
ref's ``rebar.toml``, did not recognise the current key name, and fell back to a default —
the only trace being a config warning that said "typo?" about a key that was not a typo.

Design notes:

* **Advisory only.** Reviewing from a slightly-behind checkout is legitimate. This module
  never raises, never changes a verdict, an exit code, a signature, or a provenance stamp.
  :func:`warn_if_behind` swallows everything and returns ``None`` on any problem.
* **Silent unless drift is PROVEN.** Missing build provenance, a build SHA that is not
  present in the target repo, an unresolvable pinned SHA, an absent ``git`` — all degrade
  to silence. A warning storm in dev installs would be worse than the bug.
* **Only meaningful when rebar reviews rebar.** When the gate code and the target repo are
  different repositories the build SHA simply is not an object in the target repo, so the
  ancestry probe finds nothing and this stays quiet. That is the desired behaviour, not an
  accident of implementation.
* **Direction of the config coupling.** The unknown-key wording in
  :mod:`rebar._config_coercion` needs to know whether drift was detected. Core config
  must not import the optional ``rebar.llm`` layer, so this module (high) PUSHES the flag
  down into core (low) via :func:`rebar._config_coercion.note_build_may_predate_config`.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Ceiling on every git probe. A gate must never block on a wedged git.
_GIT_TIMEOUT_S = 10

#: ``(build_sha, pinned_sha)`` pairs already warned about in this process, so a run that
#: resolves the gate handle more than once produces exactly one warning.
_WARNED: set[tuple[str, str]] = set()

_DIRTY_SUFFIX = "-dirty"


@dataclass(frozen=True)
class BuildDrift:
    """A PROVEN "the running build predates the pinned ref" finding.

    ``build_sha``/``pinned_sha`` are full 40-char SHAs (resolved in the target repo, so
    they are directly comparable); ``build_date`` is the build commit's committer date as
    ``YYYY-MM-DD``, or ``None`` when git would not report it. ``commits_behind`` is the
    number of commits on the pinned ref that the build does not have.
    """

    build_sha: str
    build_date: str | None
    pinned_sha: str
    commits_behind: int
    dirty: bool = False

    @property
    def short_build_sha(self) -> str:
        return self.build_sha[:9]

    @property
    def short_pinned_sha(self) -> str:
        return self.pinned_sha[:9]


def _git(args: list[str], repo_root: str) -> str | None:
    """Run a read-only git probe in ``repo_root``; ``None`` on any non-zero exit, empty
    output, missing git binary, or timeout. Best-effort by contract — every caller here
    treats ``None`` as "cannot establish drift", which means silence.

    ``args`` is supplied only by this module and is always one of four read-only queries:
    ``rev-parse``, ``merge-base``, ``rev-list``, ``show``. Nothing here writes, and the
    target is the repo under review, never the ticket store.
    """
    try:
        out = subprocess.run(  # raw-git-ok: read-only probes on the repo under review
            ["git", "-C", repo_root, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def _is_ancestor(ancestor: str, descendant: str, repo_root: str) -> bool:
    """True when ``ancestor`` is reachable from ``descendant``. Unlike :func:`_git` this
    needs the exit CODE (0 = yes, 1 = no), not the output, so it runs its own probe."""
    try:
        out = subprocess.run(  # fixed argv, no shell
            ["git", "-C", repo_root, "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def running_build_sha() -> str | None:
    """The commit of the rebar build executing this process, or ``None`` when unknown.

    Delegates to :func:`rebar.signing._gate_commit_sha`, which already encodes the
    resolution order this needs: the live rebar source checkout first (dev/editable
    installs), then the SHA baked into the wheel at build time by ``hatch_build.py``
    (``rebar._build_info.COMMIT``), then ``None``. Any failure is provenance-unavailable,
    which this module treats as "stay silent".
    """
    try:
        from rebar.signing import _gate_commit_sha

        return _gate_commit_sha()
    # Provenance is best-effort; never break a gate over it.
    except Exception:
        logger.debug("build drift: could not resolve the running build sha", exc_info=True)
        return None


def _resolve_repo_root(repo_root: str | None) -> str | None:
    try:
        from rebar import config as _root_config

        return str(_root_config.repo_root(repo_root))
    # No resolvable repo root means no drift check.
    except Exception:
        logger.debug("build drift: could not resolve the target repo root", exc_info=True)
        return repo_root


def detect_drift(
    pinned_sha: str | None, repo_root: str | None, *, build_sha: str | None = None
) -> BuildDrift | None:
    """Return a :class:`BuildDrift` when a build is a STRICT ancestor of ``pinned_sha``
    in the target repo, else ``None``.

    ``build_sha`` names the build to measure and DEFAULTS to the running process's own
    (:func:`running_build_sha`), which is the gate-time question this module was written
    for. It is a parameter because the same ancestry question is asked about a build the
    caller is not executing: ``rebar doctor``'s build-freshness scan measures the build
    the host's main-tracking updater PUBLISHED, read off its ``current`` pointer, which a
    developer checkout cannot answer by introspecting itself (bug ae97-a37b-9fa3-413a).
    Passing it reuses this ancestry walk rather than forking a second one that would
    drift from it.

    ``None`` covers every "cannot prove drift" case as well as the healthy ones: no build
    provenance, no pinned SHA (a ``local``-source gate pins nothing), either SHA absent
    from the target repo (notably: gate code and target are different repositories), git
    unavailable, and the build being at or ahead of the pinned ref.
    """
    raw = build_sha or running_build_sha()
    if not raw or not pinned_sha:
        return None
    dirty = raw.endswith(_DIRTY_SUFFIX)
    build_ref = raw[: -len(_DIRTY_SUFFIX)] if dirty else raw
    if not build_ref:
        return None

    root = _resolve_repo_root(repo_root)
    if not root:
        return None

    # Resolve BOTH through the target repo. This is what makes a foreign target repo
    # silent: the build sha is not an object there, so rev-parse fails and we return None.
    build_full = _git(["rev-parse", "--verify", "--quiet", f"{build_ref}^{{commit}}"], root)
    pinned_full = _git(["rev-parse", "--verify", "--quiet", f"{pinned_sha}^{{commit}}"], root)
    if not build_full or not pinned_full or build_full == pinned_full:
        return None

    if not _is_ancestor(build_full, pinned_full, root):
        return None  # ahead of, or diverged from, the pinned ref — not "behind"

    raw_count = _git(["rev-list", "--count", f"{build_full}..{pinned_full}"], root)
    try:
        commits_behind = int(raw_count) if raw_count else 0
    except ValueError:
        commits_behind = 0
    if commits_behind <= 0:
        return None

    return BuildDrift(
        build_sha=build_full,
        build_date=_git(["show", "-s", "--format=%cs", build_full], root),
        pinned_sha=pinned_full,
        commits_behind=commits_behind,
        dirty=dirty,
    )


def _note_config_wording(behind: bool) -> None:
    """Tell core config whether an unknown key might be a key this build predates."""
    try:
        from rebar._config_coercion import note_build_may_predate_config

        note_build_may_predate_config(behind=behind)
    # The wording is cosmetic; never break a gate over it.
    except Exception:
        logger.debug("build drift: could not set the config unknown-key wording", exc_info=True)


def warn_if_behind(pinned_sha: str | None, repo_root: str | None) -> BuildDrift | None:
    """Emit ONE warning when the running build predates ``pinned_sha``; return the finding.

    Deduplicated per ``(build_sha, pinned_sha)`` for the life of the process. Advisory
    only: this never raises, so a gate calls it unguarded and is unaffected by anything
    that goes wrong inside.
    """
    try:
        drift = detect_drift(pinned_sha, repo_root)
    # A drift probe must never fail a gate run.
    except Exception:
        logger.debug("build drift: drift detection failed", exc_info=True)
        return None
    # Set on EVERY resolution, not only on drift: a later gate in the same process that is
    # NOT behind must not inherit the previous one's wording.
    _note_config_wording(drift is not None)
    if drift is None:
        return None

    key = (drift.build_sha, drift.pinned_sha)
    if key in _WARNED:
        return drift
    _WARNED.add(key)

    logger.warning(
        "this rebar build (%s, %s%s) is %d commit(s) BEHIND the ref this gate pinned (%s) "
        "— it may predate recent config keys and gate fixes, so results can be wrong in "
        "ways the verdict cannot show. Re-run from a checkout at the pinned ref if a "
        "result looks off. (Advisory only: the verdict is unaffected.)",
        drift.short_build_sha,
        drift.build_date or "date unknown",
        ", dirty" if drift.dirty else "",
        drift.commits_behind,
        drift.short_pinned_sha,
    )
    return drift


def reset_warned() -> None:
    """Clear the once-per-pair dedup set and the config wording flag (tests)."""
    _WARNED.clear()
    _note_config_wording(False)
