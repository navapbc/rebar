"""The Reconcile Bridge push loop must converge under normal contention
(bug 4c4f-e554-849a-4dfd).

The `tickets` branch has many concurrent producers (the review bot pushes on every
vote, agents auto-push every ticket write, and the bridge itself runs near-continuously
under RECONCILE_CONTINUOUS). Measured from GHA run 30585738860: five remote advances in
41.9s, a mean competitor interval of ~8.4s.

Against that, the loop sleeps `(1 << attempt) + RANDOM % 3` — 3, 6, 10, 17s — AFTER it
has already fetched and merged. At that moment its HEAD is a valid fast-forward, so the
only thing that can invalidate it is another push arriving first: the sleep can only
forfeit the race it exists to win, and it grows precisely as the attempt budget shrinks.
Exponential backoff is right for load-shedding a rate-limited server; it is wrong for an
optimistic-concurrency CAS on a hot append-only ref. The workflow's own comment claims it
"mirrors the local client path (_store/push.py)" — which has no backoff at all and
re-pushes immediately after a clean merge.

The loop also computes a fifth merge and then falls out of `while` without pushing it,
discarding a fast-forwardable HEAD it had just paid for.

WHY THIS DRIVES THE WORKFLOW YAML DIRECTLY rather than an extracted script: the retry
loop contains zero `${{ }}` expressions (only the commit-message section above it has
one, `${{ github.run_id }}`), so the block can be executed verbatim. Testing the YAML
tests the artifact CI actually runs; extracting the logic to a script would introduce a
drift seam where the workflow's invocation of it becomes untested glue. The substitution
guard below fails loudly if an expression is ever added inside the block.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
import time
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "reconcile-bridge.yml"
PUSH_STEP = "Commit reconciler events back and push to origin/tickets"
# The competitor pushes before each of the loop's 5 attempts, then stops. The current
# loop therefore exhausts its budget and discards its final merge; a loop that re-pushes
# after a clean merge converges on the next try.
COMPETITOR_PUSHES = 5


def _extract_push_block() -> str:
    """Return the push step's shell, with Actions expressions substituted.

    Fails loudly on any unsubstituted `${{ }}` so that adding an expression inside the
    block cannot silently produce a test that no longer matches production.
    """
    doc = yaml.safe_load(WORKFLOW.read_text())
    steps = doc["jobs"]["reconcile"]["steps"]
    run = next(s["run"] for s in steps if s.get("name") == PUSH_STEP)
    run = run.replace("${{ github.run_id }}", "test-run-id")
    leftover = re.findall(r"\$\{\{[^}]*\}\}", run)
    assert not leftover, (
        "the push step gained an Actions expression this harness does not substitute, so "
        f"the test would no longer execute what CI executes: {leftover}"
    )
    return run


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture()
def bridge(tmp_path: Path) -> dict:
    """A bare origin with a `tickets` branch, a workspace ahead of it, and a competitor."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    # The step begins `cd .tickets-tracker`, so the tracker clone must sit at that
    # path beneath the working directory the script is invoked from.
    root = tmp_path / "root"
    root.mkdir()
    work = root / ".tickets-tracker"
    comp = tmp_path / "comp"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    subprocess.run(
        ["git", "init", "--bare", "-b", "tickets", str(origin)], check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-b", "tickets", str(seed)], check=True, capture_output=True)
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
        _git(seed, "config", k, v)
    (seed / "seed.txt").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "tickets")

    for path in (work, comp):
        subprocess.run(
            ["git", "clone", "-q", "-b", "tickets", str(origin), str(path)],
            check=True,
            capture_output=True,
        )
        for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
            _git(path, "config", k, v)

    # Workspace has a local commit -> it is ahead of origin, so the loop will push.
    (work / "event-local.json").write_text('{"local": true}\n')
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "local reconciler events")

    # `git` shim: before each `push origin HEAD:tickets`, let the competitor land first,
    # for the first COMPETITOR_PUSHES attempts. Union-by-unique-filename, so merges are
    # always clean — exactly the production invariant (docs/concurrency.md I1).
    counter = tmp_path / "n"
    counter.write_text("0")
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    shim = bin_dir / "git"
    shim.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        if [ "$1" = "push" ] && printf '%s ' "$@" | grep -q 'HEAD:tickets'; then
          n=$(cat {counter})
          if [ "$n" -lt {COMPETITOR_PUSHES} ]; then
            echo $((n + 1)) > {counter}
            f="{comp}/event-comp-$n.json"
            echo "{{\\"c\\": $n}}" > "$f"
            {real_git} -C {comp} pull -q --no-rebase origin tickets >/dev/null 2>&1
            {real_git} -C {comp} add -A >/dev/null 2>&1
            {real_git} -C {comp} commit -q -m "competitor $n" >/dev/null 2>&1
            {real_git} -C {comp} push -q origin tickets >/dev/null 2>&1
          fi
        fi
        exec {real_git} "$@"
    """)
    )
    shim.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["BRIDGE_BOT_NAME"] = "t"
    env["BRIDGE_BOT_EMAIL"] = "t@example.invalid"
    return {"work": work, "root": root, "origin": origin, "env": env, "counter": counter}


def test_push_loop_converges_under_contention(bridge: dict) -> None:
    """The loop must land its commit once the competitor stops, not exhaust and fail.

    The competitor pushes before each of the 5 attempts then stops, so a loop that
    re-pushes promptly after a clean merge converges. The current loop sleeps an
    increasing interval after each merge and then throws its final merge away.
    """
    root: Path = bridge["root"]
    script = root / "push_step.sh"
    script.write_text(_extract_push_block())

    started = time.monotonic()
    result = subprocess.run(
        ["bash", str(script)],
        cwd=root,
        env=bridge["env"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    elapsed = time.monotonic() - started
    ctx = (
        f"rc={result.returncode}\ncompetitor pushes fired="
        f"{bridge['counter'].read_text().strip()}\n"
        f"stdout:\n{result.stdout[-3000:]}\nstderr:\n{result.stderr[-3000:]}"
    )

    assert result.returncode == 0, (
        "the push loop must converge once contention stops — losing a benign race five "
        f"times must not fail the bridge red.\n{ctx}"
    )

    # The convergence assertions above are satisfied by pushing the final merge alone —
    # they do NOT constrain the backoff. Wall-clock does: the old exponential sleep costs
    # 3+6+10+17 = 30-38s across four retries, while an immediate re-push after a clean
    # merge completes the same five-way contention in ~3s. Asserting elapsed time is the
    # observable way to pin "no anti-adaptive sleep" without asserting on the script text.
    # The bound is deliberately loose (an order of magnitude over the ~3s real cost, and
    # well under the >=30s any exponential variant needs) so it cannot flake on a slow
    # runner while still failing outright if the backoff returns.
    # timing: hang-guard — backoff-return guard; 10x over the ~3s cost
    assert elapsed < 20.0, (
        f"the loop took {elapsed:.1f}s for {COMPETITOR_PUSHES} contended attempts. After a "
        "clean merge HEAD is already fast-forwardable, so a growing sleep can only forfeit "
        f"the race it exists to win.\n{ctx}"
    )

    landed = subprocess.run(
        ["git", "log", "--oneline", "tickets"],
        cwd=bridge["origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "local reconciler events" in landed, (
        "the workspace's commit must actually reach origin/tickets — a zero exit with the "
        f"commit unpushed would be worse than failing.\norigin log:\n{landed}\n{ctx}"
    )


# --- e161: the rejection classifier must not be SIGPIPE-fragile under `pipefail` -------------
#
# reconcile-bridge.yml:314 classifies a failed push with
#     if echo "$push_stderr" | grep -qiE 'non-fast-forward|rejected|fetch first'; then
# Under the step's `set -euo pipefail`, `grep -q` exits the instant it matches an early line
# (`rejected`/`fetch first` sit on stderr line 2). If `echo` has not finished writing the
# capture when the pipe's read end closes, `echo` takes SIGPIPE (141); `pipefail` then makes
# the pipeline's status 141, so the `if` wrongly takes the ELSE (non-retryable) arm even though
# the text matched. On a loaded CI runner this is a scheduling race (rare, load-only, never
# reproduces on an idle dev box). We force it deterministically by enlarging the capture past
# what `echo` can buffer before `grep -q` matches and exits — the same mechanism, made reliable.
PADDED_COMPETITOR_PUSHES = 1
_STDERR_PAD_BYTES = 200_000


@pytest.fixture()
def bridge_padded(tmp_path: Path) -> dict:
    """Like `bridge`, but the workspace's rejected push carries a large stderr capture.

    Exactly one competitor push fires (so attempt 1 is a benign rejection and attempt 2 can
    succeed), and the shim appends a `_STDERR_PAD_BYTES` blob AFTER the real rejection text so
    the classifier's `echo "$push_stderr" | grep -q` cannot drain before `grep -q` matches and
    exits — deterministically reproducing the loaded-runner SIGPIPE/pipefail misclassification.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    root = tmp_path / "root"
    root.mkdir()
    work = root / ".tickets-tracker"
    comp = tmp_path / "comp"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()

    subprocess.run(
        ["git", "init", "--bare", "-b", "tickets", str(origin)], check=True, capture_output=True
    )
    subprocess.run(["git", "init", "-b", "tickets", str(seed)], check=True, capture_output=True)
    for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
        _git(seed, "config", k, v)
    (seed / "seed.txt").write_text("seed\n")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "tickets")

    for path in (work, comp):
        subprocess.run(
            ["git", "clone", "-q", "-b", "tickets", str(origin), str(path)],
            check=True,
            capture_output=True,
        )
        for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
            _git(path, "config", k, v)

    (work / "event-local.json").write_text('{"local": true}\n')
    _git(work, "add", "-A")
    _git(work, "commit", "-m", "local reconciler events")

    counter = tmp_path / "n"
    counter.write_text("0")
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    shim = bin_dir / "git"
    # NB: the shim itself runs WITHOUT `pipefail`, so its own `printf | grep -q` guard cannot be
    # poisoned by the same SIGPIPE effect — only the workflow's classifier is under test.
    shim.write_text(
        textwrap.dedent(f"""\
        #!/usr/bin/env bash
        if [ "$1" = "push" ] && printf '%s ' "$@" | grep -q 'HEAD:tickets'; then
          n=$(cat {counter})
          if [ "$n" -lt {PADDED_COMPETITOR_PUSHES} ]; then
            echo $((n + 1)) > {counter}
            f="{comp}/event-comp-$n.json"
            echo "{{\\"c\\": $n}}" > "$f"
            {real_git} -C {comp} pull -q --no-rebase origin tickets >/dev/null 2>&1
            {real_git} -C {comp} add -A >/dev/null 2>&1
            {real_git} -C {comp} commit -q -m "competitor $n" >/dev/null 2>&1
            {real_git} -C {comp} push -q origin tickets >/dev/null 2>&1
            # The workspace push below is now a real non-fast-forward rejection. Emit its real
            # stderr FIRST (so the classifier's pattern matches early), then a large blob, so
            # `echo "$push_stderr" | grep -q` is still writing when `grep -q` matches and exits.
            err=$({real_git} "$@" 2>&1); rc=$?
            printf '%s\\n' "$err" >&2
            head -c {_STDERR_PAD_BYTES} /dev/zero | tr '\\0' x >&2
            printf '\\n' >&2
            exit $rc
          fi
        fi
        exec {real_git} "$@"
    """)
    )
    shim.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    env["BRIDGE_BOT_NAME"] = "t"
    env["BRIDGE_BOT_EMAIL"] = "t@example.invalid"
    return {"work": work, "root": root, "origin": origin, "env": env, "counter": counter}


def test_rejection_classifier_is_not_sigpipe_fragile(bridge_padded: dict) -> None:
    """A retryable rejection whose stderr is large must still be retried, not failed red.

    RED against reconcile-bridge.yml:314's `echo "$push_stderr" | grep -q` (the large capture
    forces `echo` to SIGPIPE when `grep -q` matches and exits, so `pipefail` routes a matched,
    retryable rejection into the non-retryable ELSE arm). GREEN once the classifier no longer
    depends on a pipeline exit status — the push is fetched, merged, and re-pushed to origin.
    """
    root: Path = bridge_padded["root"]
    script = root / "push_step.sh"
    script.write_text(_extract_push_block())

    result = subprocess.run(
        ["bash", str(script)],
        cwd=root,
        env=bridge_padded["env"],
        capture_output=True,
        text=True,
        timeout=300,
    )
    # Prove the precondition: exactly one competitor push fired, so attempt 1 WAS a benign
    # rejection that the loop had to retry (not a spurious failure of the harness).
    assert bridge_padded["counter"].read_text().strip() == str(PADDED_COMPETITOR_PUSHES), (
        "the competitor must have landed exactly once, forcing a single retryable rejection"
    )
    ctx = (
        f"rc={result.returncode}\n"
        f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}"
    )
    assert result.returncode == 0, (
        "a rejected push with a large stderr must be classified RETRYABLE, not non-retryable "
        "— the classifier must not depend on a `pipefail`-poisoned pipeline exit status.\n"
        f"{ctx}"
    )
    landed = subprocess.run(
        # `-c safe.bareRepository=all`: some dev machines set `safe.bareRepository=explicit`
        # globally, which makes a plain `git log` refuse the bare origin (exit 128); CI does
        # not. Neutralize it for this read so the assertion reflects convergence, not env.
        ["git", "-c", "safe.bareRepository=all", "log", "--oneline", "tickets"],
        cwd=bridge_padded["origin"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert "local reconciler events" in landed, (
        "the workspace commit must reach origin/tickets after the retry.\n"
        f"origin log:\n{landed}\n{ctx}"
    )
