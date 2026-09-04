"""One owner for the "this change predates the gate" invariant.

A patchset whose base predates a gate script must not fail ``Verified`` on that gate, so
every CI step that runs a repo-local gate file guards it with
``[ -f <path> ] … "change predates the gate" … skip``. That guard is written 24 times across
``.github/`` and it CANNOT be factored into a shared repo file: any such owner — a composite
action, a shell helper, a Python module — is itself absent on exactly the pre-gate trees the
guard serves, so the guard would skip gates whose scripts DO exist (or the job would fail
resolving a missing local ``uses:``). ``gerrit-verify.yaml`` already carries that exposure for
``uses: ./.github/actions/docs-gates`` itself.

What CAN be shared is the invariant. Two copies of this guard drifted apart before —
``anxious-resistant-urson`` (a gate step with no guard) and ``sapphire-vulnerable-fruitfly``
(a guard added late) — so this module is the single place that pins it:

* **completeness** — every step that runs a repo-local gate file either guards that exact file
  or carries a ``# predates-gate-ok: <reason>`` marker with a non-empty reason;
* **behaviour** — every guarded step body, run in an EMPTY directory, exits 0 and says why.

The behaviour half generalises
``test_ci_workflow_parity.py::test_mutation_selector_skips_pre_gate_trees_without_weakening_current_trees``
from the one mutation selector to every guarded step. These tests assert the GUARD, never a
step's shape, so the four sites that carry extra behaviour on the skip path (the mutation
selector's ``$GITHUB_OUTPUT`` writes, ``_optionality.yml``'s two independently guarded
generators per bug ``5bc6-5496-4e17-403a``, the inverted ``[ -f ]/else`` forms, and the
``git-version-floor.txt`` data-file guard) keep their current bodies.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import yaml
from _subprocess_env import subprocess_env

_ROOT = Path(__file__).resolve().parents[2]

#: The files whose steps run against an arbitrary patchset, so a base older than a gate file
#: is reachable in them. Every one of these already carries at least one guard today.
GUARDED_WORKFLOWS: tuple[str, ...] = (
    ".github/workflows/_build-and-test.yml",
    ".github/workflows/_optionality.yml",
    ".github/workflows/_mutation.yml",
    ".github/workflows/gerrit-verify.yaml",
    ".github/workflows/test.yml",
    ".github/actions/docs-gates/action.yml",
)

#: The canonical skip message. Both halves of the invariant key off this exact phrase, so a
#: copy that invents its own wording is a completeness failure rather than a silent variant.
SKIP_MESSAGE = "change predates the gate"

#: A repo-local file a step executes or reads. Restricted to the directories that actually
#: hold gate material; a hit only counts when the path EXISTS in the tree today, which is
#: what makes "absent on an older base" the question the guard answers.
_REPO_PATH = re.compile(
    r"(?<![\w/.-])((?:scripts|tests|docs)/[\w./-]*\.(?:py|txt|json)"
    r"|\.github/[\w./-]*\.(?:txt|json))"
)

#: ``name=path`` on a line of its own — the ``floorfile=.github/git-version-floor.txt`` form,
#: whose guard tests ``"$floorfile"`` rather than the literal path.
_SHELL_ASSIGN = re.compile(r"^\s*(\w+)=([\w./-]+)\s*$", re.MULTILINE)

#: ``${{ … }}`` — inert when a step body is executed outside Actions.
_ACTIONS_EXPR = re.compile(r"\$\{\{[^}]*\}\}")

#: The escape hatch. The reason is MANDATORY: a bare marker must not silence the check.
_MARKER = re.compile(r"#\s*predates-gate-ok:[ \t]*(.*)")


def _expand_assignments(run: str) -> str:
    """``run`` with simple ``name=path`` shell assignments substituted into their uses."""
    body = run
    for name, value in _SHELL_ASSIGN.findall(run):
        body = body.replace(f"${{{name}}}", value).replace(f"${name}", value)
    return body


def marker_reason(run: str) -> str | None:
    """The ``# predates-gate-ok:`` reason in ``run``, or ``None`` when it carries no marker.

    An empty string means the marker is present but unreasoned — a violation, not an excuse.
    """
    match = _MARKER.search(run)
    return match.group(1).strip() if match else None


def unguarded_paths(run: str, *, existing: set[str] | None = None) -> list[str]:
    """Repo-local gate files ``run`` uses without an ``-f`` guard naming that same file."""
    body = _expand_assignments(_ACTIONS_EXPR.sub("", run))
    found = sorted(set(_REPO_PATH.findall(body)))
    if existing is not None:
        found = [path for path in found if path in existing]
    return [path for path in found if not re.search(rf'-f\s+"?{re.escape(path)}', body)]


def step_violation(run: str, *, existing: set[str] | None = None) -> str | None:
    """Why ``run`` breaks the invariant, or ``None`` when it holds."""
    reason = marker_reason(run)
    if reason == "":
        return "carries a `# predates-gate-ok:` marker with no reason"
    if reason:
        return None
    missing = unguarded_paths(run, existing=existing)
    if missing:
        return f"runs {', '.join(missing)} with no `-f` guard and no marker"
    return None


def _run_steps(path: str) -> list[tuple[str, str, str]]:
    """``(job, step name, run body)`` for every ``run:`` step in a workflow or action file."""
    document: dict[str, Any] = yaml.safe_load((_ROOT / path).read_text(encoding="utf-8"))
    jobs = list((document.get("jobs") or {}).items())
    jobs.append(("runs", document.get("runs") or {}))
    steps: list[tuple[str, str, str]] = []
    for job_name, job in jobs:
        for step in job.get("steps") or []:
            if isinstance(step.get("run"), str):
                steps.append((job_name, str(step.get("name", "<unnamed>")), step["run"]))
    return steps


#: The git listing that answers "which gate files does this tree have?". ``--cached
#: --others --exclude-standard`` is load-bearing (bug ``1035-bed7-c855-4732``): a
#: tracked-only listing cannot see a gate file that has been WRITTEN but not yet
#: ``git add``ed, so this check reported green before staging and red after — on one
#: unchanged working tree. That is a second way the local signal disagrees with CI, and
#: it is the more dangerous way, because the green arrives first.
_LS_FILES = ("git", "ls-files", "--cached", "--others", "--exclude-standard", "--")
_GATE_DIRS = ("scripts", "tests", "docs", ".github")


def _existing_paths(root: Path) -> set[str]:
    """Repo-local gate files in ``root``, as CI will see them once the tree is committed.

    Widening from tracked-only to tracked-plus-untracked can only turn a FALSE GREEN red:
    every path it adds is one more step that must carry a guard, never one fewer.
    ``--exclude-standard`` keeps ``.gitignore``d build output (``.venv``, ``.tools``,
    caches) out, so the set stays the files a commit would actually carry.
    """
    listing = subprocess.run(
        [*_LS_FILES, *_GATE_DIRS],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(listing.stdout.split())


def test_completeness_every_gate_step_is_guarded_or_marked() -> None:
    """No step runs a repo-local gate file unguarded and unexplained."""
    existing = _existing_paths(_ROOT)
    violations = [
        f"{path} :: [{job}] {name!r} {problem}"
        for path in GUARDED_WORKFLOWS
        for job, name, run in _run_steps(path)
        if (problem := step_violation(run, existing=existing))
    ]
    assert not violations, (
        "these CI steps run a repo-local gate file that an older base may not have.\n"
        "Guard it with `[ -f <path> ]` + the canonical skip message, or record why no guard\n"
        "is needed with `# predates-gate-ok: <reason>`:\n  " + "\n  ".join(violations)
    )


def test_empty_tree_every_guarded_step_skips_cleanly(tmp_path: Path) -> None:
    """Every guarded body is self-sufficient: exit 0 and say why, on a tree with nothing in it.

    This is what forbids a shared owner. A body that shelled out to a repo file would fail
    here, because on a pre-gate tree that file is exactly as absent as the gate script.
    """
    guarded = [
        (path, job, name, run)
        for path in GUARDED_WORKFLOWS
        for job, name, run in _run_steps(path)
        if SKIP_MESSAGE in run
    ]
    assert len(guarded) >= 20, f"expected the guard across the lane, found {len(guarded)} steps"

    failures = []
    for index, (path, job, name, run) in enumerate(guarded):
        empty_tree = tmp_path / f"predates-{index}"
        empty_tree.mkdir()
        github_output = empty_tree / "github-output"
        step_summary = empty_tree / "step-summary"
        github_output.touch()
        step_summary.touch()
        result = subprocess.run(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _ACTIONS_EXPR.sub("", run)],
            cwd=empty_tree,
            env=subprocess_env(
                {
                    "GITHUB_OUTPUT": str(github_output),
                    "GITHUB_STEP_SUMMARY": str(step_summary),
                }
            ),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0 or SKIP_MESSAGE not in result.stdout:
            failures.append(
                f"{path} :: [{job}] {name!r} exited {result.returncode}\n"
                f"{(result.stdout + result.stderr)[-400:]}"
            )
    assert not failures, "\n".join(failures)


def test_a_step_that_drops_its_guard_is_caught() -> None:
    """The completeness check fails an unguarded gate step — it is not vacuous."""
    assert step_violation("python scripts/check_comment_hygiene.py") is not None
    guarded = (
        "if [ ! -f scripts/check_comment_hygiene.py ]; then\n"
        f'  echo "scripts/check_comment_hygiene.py not in tree ({SKIP_MESSAGE}) — skipping"\n'
        "  exit 0\nfi\npython scripts/check_comment_hygiene.py\n"
    )
    assert step_violation(guarded) is None


def test_a_marker_without_a_reason_is_rejected() -> None:
    """The escape hatch costs an explanation; a bare marker does not buy silence."""
    unguarded = "python scripts/check_comment_hygiene.py"
    assert step_violation(f"# predates-gate-ok:\n{unguarded}") is not None
    assert step_violation(f"# predates-gate-ok: gated by the guarded selector\n{unguarded}") is None


def test_golden_path_guards_match_the_verified_lane() -> None:
    """Both lanes guard the golden-path gate scripts (bug e818-564e-d3b6-4eaa).

    ``gerrit-verify.yaml`` grew the guards first and ``test.yml``'s twin steps stayed bare —
    the one-lane-only drift this module exists to prevent. Pin that every step running either
    script, in EITHER lane, carries its own ``-f`` guard and the canonical skip message.
    """
    scripts = ("scripts/check_readme_quickstart.py", "scripts/probe_rebar.py")
    lanes = (".github/workflows/test.yml", ".github/workflows/gerrit-verify.yaml")
    for lane in lanes:
        runs = {
            script: [run for _, _, run in _run_steps(lane) if script in run] for script in scripts
        }
        for script, bodies in runs.items():
            assert bodies, f"{lane} no longer runs {script} — this guard's anchor is stale"
            for body in bodies:
                assert not unguarded_paths(body), (
                    f"{lane} runs {script} with no `-f` guard — the twin lane guards it, so a "
                    "base older than the script reddens only this lane"
                )
                assert SKIP_MESSAGE in body, (
                    f"{lane}'s guard for {script} does not use the canonical skip message"
                )


def test_a_written_but_unstaged_gate_file_is_already_visible(tmp_path: Path) -> None:
    """The guard's verdict is a property of the TREE, not of the index (bug ``1035``).

    The completeness check only asks about paths that EXIST, so what counts as existing
    decides the verdict. While that was a tracked-only listing, an author who wrote a new
    gate script and ran the check before ``git add`` got a green that flipped red the
    moment they staged the very same file — a locally-green tree that CI rejects, which is
    the defect class this module already exists to prevent, arriving through the index
    instead of through a missing guard.
    """
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "."], cwd=repo, check=True)
    (repo / "scripts" / "check_tracked.py").write_text("", encoding="utf-8")
    subprocess.run(["git", "add", "scripts/check_tracked.py"], cwd=repo, check=True)
    (repo / "scripts" / "check_unstaged.py").write_text("", encoding="utf-8")
    (repo / ".gitignore").write_text("scripts/check_ignored.py\n", encoding="utf-8")
    (repo / "scripts" / "check_ignored.py").write_text("", encoding="utf-8")

    before_staging = _existing_paths(repo)
    assert "scripts/check_unstaged.py" in before_staging, (
        "a written-but-unstaged gate file must already count as present, or running this "
        "check before `git add` reports a green that staging turns red"
    )
    assert "scripts/check_ignored.py" not in before_staging, (
        "a .gitignore'd path is not in the tree a commit carries, so it must not be "
        "demanded of a guard"
    )

    subprocess.run(["git", "add", "scripts/check_unstaged.py"], cwd=repo, check=True)
    assert _existing_paths(repo) == before_staging, (
        "staging changed the verdict — the tracked-only skew this test pins is back"
    )
