"""The one git failure classifier and its two uniqueness guards (story d6e3-548c-37eb-4c45).

The store used to classify git stderr at five sites with five private marker tables, and
the SAME text got different verdicts at each. These tests pin three things:

1. **the registry** — every marker resolves to exactly one kind FOR A GIVEN OPERATION, and
   the three deliberately different verdicts for ``cannot lock ref`` are PRESERVED (merging
   them would re-open bugs 4afc and ebee);
2. **the actions** — a kind still drives the same behaviour at each caller: a transient
   runner-FS fault is retried by the sync and push-recovery merges, and the invalid-object
   kind still triggers the index-rebuild RECOVERY rather than failing the write;
3. **the guards** — the marker strings live only in the owner module, and the synthetic
   rc-124 timeout result is CONSTRUCTED in only one place.

Both guards are exercised against synthetic trees as well as the real one, so a guard that
can no longer fail is itself caught.
"""

from __future__ import annotations

import ast
import logging as _logging
import subprocess
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from rebar._store import event_append, git_outcome, push_classify, push_recovery, sync
from rebar._store.git_outcome import GitKind
from rebar._store.gitutil import _is_git_lock_error, _is_transient_git_fault

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src" / "rebar"

# ── The table: one captured stderr per kind, under the operation it arises in ──────────
# Each is a real git message (or the synthetic result rebar itself builds), not a paraphrase.
TABLE: tuple[tuple[str, str, GitKind], ...] = (
    (
        git_outcome.LOCAL,
        "fatal: Unable to create '/t/.git/index.lock': File exists.\n"
        "Another git process seems to be running in this repository",
        GitKind.LOCK,
    ),
    (git_outcome.LOCAL, "fatal: could not parse HEAD", GitKind.TRANSIENT_FS),
    (git_outcome.LOCAL, "fatal: bad object 6f1a2b3", GitKind.TRANSIENT_FS),
    (
        git_outcome.LOCAL,
        "error: unable to create temporary file: No such file or directory",
        GitKind.TRANSIENT_FS,
    ),
    (
        git_outcome.COMMIT,
        "error: invalid object 100644 4b825dc for 'x.json'\nfatal: Error building trees",
        GitKind.INVALID_OBJECT,
    ),
    (
        git_outcome.COMMIT,
        "error: Committing is not possible because you have unmerged files.",
        GitKind.UNMERGED,
    ),
    (
        git_outcome.PUSH,
        "! [rejected]        HEAD -> tickets (non-fast-forward)",
        GitKind.NON_FF,
    ),
    (
        git_outcome.PUSH,
        "! [remote rejected] HEAD -> tickets (pre-receive hook declined)",
        GitKind.POLICY_DECLINE,
    ),
    (
        git_outcome.PUSH,
        "fatal: unable to access 'https://x/': server certificate verification failed. "
        "CAfile: none CRLfile: none",
        GitKind.TRANSPORT,
    ),
    (git_outcome.PUSH, "git timed out after 30s", GitKind.TRANSPORT),
    (
        git_outcome.PUSH,
        "error: Your local changes to the following files would be overwritten by merge",
        GitKind.DIRTY_WD,
    ),
    (
        git_outcome.LEASE_PUSH,
        "! [rejected] refs/reconciler/lock (stale info)",
        GitKind.CAS_MISMATCH,
    ),
    (git_outcome.PUSH, "fatal: something nobody has ever seen", GitKind.FATAL),
)


@pytest.mark.parametrize(("operation", "stderr", "expected"), TABLE)
def test_marker_resolves_to_exactly_one_kind(
    operation: str, stderr: str, expected: GitKind
) -> None:
    """Each captured stderr classifies to exactly one kind under its operation."""
    result = subprocess.CompletedProcess(["git", "push"], 1, "", stderr)
    assert git_outcome.classify(result, operation=operation).kind is expected


def test_policy_decline_wins_over_a_bare_rejected() -> None:
    """The subtractive rule, applied once: a stderr carrying BOTH ``rejected`` and a
    policy marker is POLICY_DECLINE, never NON_FF — bug 2a76, where classifying a
    permanent hook decline as a non-fast-forward burned all three retries on the remote
    and then reported only "failed after 3 retries"."""
    both = "! [remote rejected] HEAD -> tickets (push declined due to repository rule violations)"
    assert git_outcome.classify(both, operation=git_outcome.PUSH).kind is GitKind.POLICY_DECLINE


def test_policy_decline_also_wins_over_a_transport_marker() -> None:
    """Bug f61c's other half: a policy decline is PERMANENT, so it must not be retried as
    a transport blip even when the same text names one."""
    both = "fatal: unable to access 'https://x/': Empty reply from server\nremote: push declined"
    assert git_outcome.classify(both, operation=git_outcome.PUSH).kind is GitKind.POLICY_DECLINE


def test_a_github_push_protection_code_alone_is_a_policy_decline() -> None:
    """GitHub's push-protection codes (GH006 / GH013) are the whole signal on their own —
    secret scanning rejects with the code and no other policy phrase, and retrying it hits
    a permanent rule three more times (bug 2a76)."""
    gh013 = "remote: error GH013: Repository rule violations found for refs/heads/tickets"
    assert git_outcome.classify(gh013, operation=git_outcome.PUSH).kind is GitKind.POLICY_DECLINE
    assert push_classify._is_non_fast_forward(f"! [remote rejected] tickets\n{gh013}") is False


# ── `cannot lock ref` keeps its three verdicts ────────────────────────────────────────

CANNOT_LOCK_REF = "cannot lock ref 'refs/heads/tickets': is at aaa but expected bbb"


def test_cannot_lock_ref_is_a_retriable_lock_locally() -> None:
    """Under ``local`` it is a ref-lock conflict rebar rides out — git holds ref locks for
    microseconds, so a retry has a real chance."""
    assert git_outcome.classify(CANNOT_LOCK_REF, operation=git_outcome.LOCAL).kind is GitKind.LOCK
    assert _is_git_lock_error(CANNOT_LOCK_REF), "gitutil's predicate must agree with the registry"


def test_cannot_lock_ref_is_a_lease_mismatch_on_a_lease_push() -> None:
    """Under ``lease-push`` it counts as a lease mismatch ONLY because nothing in the text
    names a non-lease cause — the subtractive posture bug 4afc established."""
    outcome = git_outcome.classify(CANNOT_LOCK_REF, operation=git_outcome.LEASE_PUSH)
    assert outcome.kind is GitKind.CAS_MISMATCH


@pytest.mark.parametrize(
    ("excluding", "reason"),
    [
        ("hook declined", "a hook decline is not lease movement (bug 4afc)"),
        ("rate limit", "a rate limit is not lease movement (bug 4afc)"),
        ("internal server error", "a 5xx is not lease movement (bug 4afc)"),
        ("fatal error in commit_refs", "a GitHub ref-transaction fault (bug ebee)"),
        ("File exists", "remote-side ref.lock contention, not lease movement"),
    ],
)
def test_cannot_lock_ref_is_not_a_lease_mismatch_when_a_non_lease_cause_is_named(
    excluding: str, reason: str
) -> None:
    """The bug-hardened exclusions survive the consolidation: each of these, alongside
    ``cannot lock ref``, must NOT be classified as lease movement."""
    text = f"{CANNOT_LOCK_REF}\nremote: {excluding}".lower()
    assert git_outcome.classify(text, operation=git_outcome.LEASE_PUSH).kind is not (
        GitKind.CAS_MISMATCH
    ), reason


def test_cannot_lock_ref_is_a_held_lock_under_ref_cas() -> None:
    """Under ``ref-cas`` the same string, with an exit-1 ``update-ref -d``, means the
    advisory lock is HELD — a terminal verdict, reached through the STRUCTURAL predicate
    rather than a marker row."""
    exc = subprocess.CalledProcessError(1, ["git", "update-ref", "-d", "refs/heads/tickets", "aaa"])
    exc.stderr = CANNOT_LOCK_REF
    assert git_outcome.classify(exc, operation=git_outcome.REF_CAS).kind is GitKind.CAS_MISMATCH


def test_the_three_verdicts_are_distinct() -> None:
    """The point of the operation key: one string, three answers that are not merged."""
    exc = subprocess.CalledProcessError(1, ["git", "update-ref", "-d", "refs/heads/tickets", "aaa"])
    exc.stderr = CANNOT_LOCK_REF
    local = git_outcome.classify(CANNOT_LOCK_REF, operation=git_outcome.LOCAL).kind
    lease = git_outcome.classify(
        f"{CANNOT_LOCK_REF}\nremote: hook declined", operation=git_outcome.LEASE_PUSH
    ).kind
    ref_cas = git_outcome.classify(exc, operation=git_outcome.REF_CAS).kind
    assert len({local, lease, ref_cas}) == 3, "the three verdicts must stay distinct"


# ── The structural predicate: exit code x command shape ───────────────────────────────


@pytest.mark.parametrize(
    ("returncode", "cmd", "stderr", "expected"),
    [
        (128, ["git", "update-ref", "refs/heads/tickets", "new", "old"], "", True),
        (1, ["git", "update-ref", "-d", "refs/heads/tickets", "old"], CANNOT_LOCK_REF, True),
        (1, ["git", "update-ref", "-d", "refs/heads/tickets", "old"], "fatal: elsewhere", False),
        (128, ["git", "merge", "refs/heads/tickets"], CANNOT_LOCK_REF, False),
    ],
    ids=["exit128-update-ref", "exit1-with-marker", "exit1-without-marker", "other-command"],
)
def test_ref_cas_discriminates_on_exit_code_and_command_shape(
    returncode: int, cmd: list[str], stderr: str, expected: bool
) -> None:
    """All four combinations of (exit code x command shape). Exit 128 on an ``update-ref``
    naming the ref is a CAS mismatch; exit 1 needs the ref-lock marker too; an exit-128
    from some OTHER git command is not a retryable race."""
    exc = subprocess.CalledProcessError(returncode, cmd)
    exc.stderr = stderr
    is_cas = git_outcome.classify(exc, operation=git_outcome.REF_CAS).kind is GitKind.CAS_MISMATCH
    assert is_cas is expected
    assert git_outcome.is_cas_mismatch(exc) is expected, "the seam and the registry must agree"


# ── The kinds still drive the same actions ────────────────────────────────────────────

TRANSIENT_STDERR = "fatal: could not parse HEAD"


def _fake_git(fail_once_on: str, stderr: str):
    """A ``_git`` stand-in that fails the FIRST invocation containing *fail_once_on*."""
    calls: list[tuple[str, ...]] = []
    state = {"failed": False}

    def _git(base, *args, **kw):
        calls.append(tuple(str(a) for a in args))
        if fail_once_on in args and not state["failed"]:
            state["failed"] = True
            return subprocess.CompletedProcess(["git", *args], 128, "", stderr)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    return _git, calls


def test_sync_union_merge_retries_a_transient_fs_fault(monkeypatch: pytest.MonkeyPatch) -> None:
    """A runner-FS blip aborts the merge before it writes anything, so the identical merge
    succeeds on retry. Without the shared retry the blip fell into the abort-and-warn path
    and a converged sync was abandoned."""
    fake, calls = _fake_git("merge", TRANSIENT_STDERR)
    monkeypatch.setattr(sync, "_git", fake)
    monkeypatch.setattr(sync.compat, "store_epoch_merge_target", lambda t, r: ("abc123", None))
    monkeypatch.setattr("rebar._store.gitutil._backoff_sleep", lambda s: None)

    sync._union_merge("/t", "origin/tickets")

    merges = [c for c in calls if c and c[0] == "merge" and "--abort" not in c]
    assert len(merges) == 2, "the transient merge must be retried once"
    assert not [c for c in calls if "--abort" in c], "a retried transient must not abort the merge"


def test_sync_union_merge_does_not_retry_a_real_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    """The retry stays narrow: a genuine conflict is NOT transient, so it still aborts on
    the first failure rather than hammering the same merge."""
    fake, calls = _fake_git("merge", "CONFLICT (content): Merge conflict in 1234/x.json")
    monkeypatch.setattr(sync, "_git", fake)
    monkeypatch.setattr(sync.compat, "store_epoch_merge_target", lambda t, r: ("abc123", None))
    monkeypatch.setattr("rebar._store.gitutil._backoff_sleep", lambda s: None)

    sync._union_merge("/t", "origin/tickets")

    merges = [c for c in calls if c and c[0] == "merge" and "--abort" not in c]
    assert len(merges) == 1, "a real conflict must not be retried"
    assert [c for c in calls if "--abort" in c], "a real conflict must still abort"


def _recovery_core(fail_stderr: str):
    calls: list[tuple[str, ...]] = []
    state = {"n": 0}

    def _git(base, *args, **kw):
        calls.append(tuple(str(a) for a in args))
        if args and args[0] == "merge" and "--abort" not in args:
            state["n"] += 1
            if state["n"] == 1:
                return subprocess.CompletedProcess(["git", *args], 128, "", fail_stderr)
        return subprocess.CompletedProcess(["git", *args], 0, "", "")

    return SimpleNamespace(_git=_git, logger=_logging.getLogger("t")), calls


def test_push_recovery_merge_retries_a_transient_fs_fault() -> None:
    """The push-recovery merge earns the same self-heal the sync merge does — it was the
    other caller left out when the transient retry was added to the s3 doctor only."""
    core, calls = _recovery_core(TRANSIENT_STDERR)
    merge = push_recovery._merge_with_transport_retry(
        core, "/t", "origin/tickets", "abc", lambda s: None
    )
    assert merge.returncode == 0, "the retried merge must succeed"
    assert len([c for c in calls if c and c[0] == "merge" and "--abort" not in c]) == 2


def test_push_recovery_merge_still_retries_a_transport_fault() -> None:
    """Bug f61c's contract is unchanged by the consolidation."""
    core, calls = _recovery_core("fatal: unable to access 'https://x/': Empty reply from server")
    merge = push_recovery._merge_with_transport_retry(
        core, "/t", "origin/tickets", "abc", lambda s: None
    )
    assert merge.returncode == 0
    assert len([c for c in calls if c and c[0] == "merge" and "--abort" not in c]) == 2


def test_push_recovery_merge_does_not_retry_a_conflict() -> None:
    """A genuine merge conflict is neither transport nor transient, and stays terminal on
    the first failure."""
    core, calls = _recovery_core("CONFLICT (content): Merge conflict in 1234/x.json")
    merge = push_recovery._merge_with_transport_retry(
        core, "/t", "origin/tickets", "abc", lambda s: None
    )
    assert merge.returncode != 0
    assert len([c for c in calls if c and c[0] == "merge" and "--abort" not in c]) == 1


INVALID_OBJECT_STDERR = (
    "error: invalid object 100644 4b825dc642cb6eb9a060e54bf8d69288fbee4904 for 'x/y.json'\n"
    "fatal: Error building trees"
)


def test_invalid_object_is_its_own_kind_and_keeps_the_rebuild_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """INVALID_OBJECT must NOT be folded into FATAL: its action is a RECOVERY (rebuild the
    index from HEAD, re-stage, retry), so folding it would turn a self-healing
    vanished-object commit into an outright write failure (bug 4c1c).

    Inject the exact stderr into a commit and assert the write still lands, having gone
    through the index rebuild."""
    assert (
        git_outcome.classify(INVALID_OBJECT_STDERR, operation=git_outcome.COMMIT).kind
        is GitKind.INVALID_OBJECT
    )

    tracker = _tracker(tmp_path)
    seen: list[str] = []
    real_run = event_append._run_git
    real_commit = event_append._git_commit
    state = {"commits": 0}

    def fake_commit(trk, msg):
        state["commits"] += 1
        if state["commits"] == 1:
            return subprocess.CompletedProcess(["git", "commit"], 1, "", INVALID_OBJECT_STDERR)
        return real_commit(trk, msg)

    def fake_run(argv):
        if "read-tree" in argv:
            seen.append("read-tree")
        return real_run(argv)

    monkeypatch.setattr(event_append, "_git_commit", fake_commit)
    monkeypatch.setattr(event_append, "_run_git", fake_run)
    rc = event_append.stage_and_commit(tracker, "tk-1", _event("u1"))

    assert rc == 0, "an invalid-object commit must self-heal, not fail the write"
    assert seen == ["read-tree"], "the recovery must rebuild the index from HEAD"


def _tracker(tmp_path: Path) -> str:
    import rebar
    from rebar import config

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    tracker = str(config.tracker_dir(str(repo)))
    event_append.stage_and_commit(tracker, "tk-0", _event("u0"))
    return tracker


def _event(uuid: str) -> dict:
    return {
        "timestamp": 1700000000000000000,
        "uuid": uuid,
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": "x"},
    }


def test_push_classify_predicates_are_registry_lookups() -> None:
    """The push loop's predicates still answer, and answer from the shared registry — the
    tables moved, they did not fork."""
    assert push_classify._is_non_fast_forward("! [rejected] (non-fast-forward)")
    assert push_classify._is_transport_retriable("git timed out after 30s")
    assert _is_transient_git_fault(TRANSIENT_STDERR)


def test_a_policy_decline_is_subtracted_from_both_retriable_predicates() -> None:
    """The SUBTRACTIVE rule where it is load-bearing: the push loop asks these two
    predicates directly, and a ``False`` from each is what makes a permanent decline
    terminal after ONE attempt instead of burning the retry budget on the remote (bug
    2a76 for the non-FF path, bug f61c for the transport one). Both texts carry the broad
    marker AND a policy cause, so each predicate must subtract."""
    declined_ff = "! [remote rejected] HEAD -> tickets (pre-receive hook declined)"
    assert push_classify._is_non_fast_forward(declined_ff.replace("hook declined", "")) is True
    assert push_classify._is_non_fast_forward(declined_ff) is False

    transport = "fatal: unable to access 'https://x/': Empty reply from server"
    assert push_classify._is_transport_retriable(transport) is True
    assert push_classify._is_transport_retriable(transport + "\nremote: hook declined") is False


# ── Guard A: the marker strings live in ONE module ────────────────────────────────────

MARKER_ATOMS = ("index.lock", "non-fast-forward", "cannot lock ref", "could not parse head")
MARKER_ESCAPE = "# git-marker-ok:"
GUARD_A_DIRS = ("_store", "_engine/rebar_reconciler")
MARKER_OWNER = "_store/git_outcome.py"


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of the Constant nodes that are DOCSTRINGS — prose, never a marker table."""
    out: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def _escaped_lines(source: str, escape: str) -> set[int]:
    """1-based line numbers sanctioned by *escape* with a MANDATORY reason: the escape
    line itself and the line after it, so a marker can be tagged in place or above."""
    lines = set()
    for i, line in enumerate(source.splitlines(), start=1):
        if escape not in line:
            continue
        reason = line.split(escape, 1)[1].strip()
        if not reason:
            raise AssertionError(f"{escape} on line {i} carries no reason")
        lines.update({i, i + 1})
    return lines


def marker_violations(source: str) -> list[tuple[int, str]]:
    """String LITERALS (never docstrings, never comments) carrying a marker atom, minus
    the escaped lines. Comments and docstrings are prose that mentions these strings all
    over rebar; only a literal can BE a marker table."""
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    escaped = _escaped_lines(source, MARKER_ESCAPE)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings or node.lineno in escaped:
            continue
        low = node.value.lower()
        for atom in MARKER_ATOMS:
            if atom in low:
                out.append((node.lineno, atom))
    return out


def test_marker_strings_live_only_in_the_owner_module() -> None:
    """Guard A. Every git marker string in the store and the reconciler's lock modules
    belongs to ``git_outcome.py``; anything else needs a reasoned escape."""
    offenders: dict[str, list[tuple[int, str]]] = {}
    for rel in GUARD_A_DIRS:
        for path in sorted((SRC / rel).rglob("*.py")):
            if path.relative_to(SRC).as_posix() == MARKER_OWNER:
                continue
            hits = marker_violations(path.read_text(encoding="utf-8"))
            if hits:
                offenders[path.relative_to(SRC).as_posix()] = hits
    assert not offenders, f"git marker strings outside {MARKER_OWNER}: {offenders}"


def test_guard_a_catches_a_new_private_marker_table() -> None:
    """The guard can FAIL — a fork of the table in another module is caught."""
    src = textwrap.dedent('''
        """A module that mentions cannot lock ref in prose."""
        MY_MARKERS = ("cannot lock ref", "boom")
    ''')
    assert marker_violations(src) == [(3, "cannot lock ref")]


def test_guard_a_ignores_comments_and_docstrings() -> None:
    """Prose is not a marker table: the guard must not fire on the dozens of comments and
    docstrings across rebar that explain these strings."""
    src = textwrap.dedent('''
        """Explains index.lock contention and could not parse HEAD at length."""
        # non-fast-forward is handled elsewhere; cannot lock ref too.
        X = 1
    ''')
    assert marker_violations(src) == []


def test_guard_a_escape_requires_a_reason() -> None:
    """An escape with no reason is itself a failure."""
    ok = 'X = "cannot lock ref"  # git-marker-ok: parsed for a hint, not a verdict\n'
    assert marker_violations(ok) == []
    with pytest.raises(AssertionError):
        marker_violations('X = "cannot lock ref"  # git-marker-ok:\n')


# ── Guard B: the synthetic rc-124 timeout result is CONSTRUCTED once ──────────────────

TIMEOUT_ESCAPE = "# git-timeout-ok:"
TIMEOUT_OWNER = "gitutil.py"


def _carries(node: ast.AST, text: str) -> bool:
    """Whether *node* is a str constant or f-string containing *text*."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return text in node.value.lower()
    if isinstance(node, ast.JoinedStr):
        return any(_carries(v, text) for v in node.values)
    return False


def timeout_constructions(source: str) -> list[int]:
    """Calls to ``subprocess.CompletedProcess`` carrying BOTH the literal 124 AND a
    ``timed out after`` string — the CONJUNCTION that makes it a construction rather than
    the ``"git timed out after"`` marker ROW, which stays a legitimate table entry."""
    tree = ast.parse(source)
    escaped = _escaped_lines(source, TIMEOUT_ESCAPE)
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (
            node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        )
        if name != "CompletedProcess" or node.lineno in escaped:
            continue
        args = list(node.args) + [kw.value for kw in node.keywords]
        has_124 = any(isinstance(a, ast.Constant) and a.value == 124 for a in args)
        if has_124 and any(_carries(a, "timed out after") for a in args):
            out.append(node.lineno)
    return out


def test_synthetic_timeout_is_constructed_only_in_the_owner() -> None:
    """Guard B. Exactly one construction in the store, and it is the shared runner's."""
    found = {
        path.relative_to(SRC).as_posix(): timeout_constructions(path.read_text(encoding="utf-8"))
        for path in sorted((SRC / "_store").rglob("*.py"))
    }
    owner = [p for p, lines in found.items() if lines and p.endswith(TIMEOUT_OWNER)]
    others = {p: lines for p, lines in found.items() if lines and not p.endswith(TIMEOUT_OWNER)}
    assert not others, f"synthetic-timeout constructions outside {TIMEOUT_OWNER}: {others}"
    assert len(owner) == 1 and len(found[owner[0]]) == 1, f"expected one construction, got {found}"


def test_guard_b_catches_a_reintroduced_shim() -> None:
    """The guard can FAIL — a private timeout fold coming back is caught."""
    src = textwrap.dedent("""
        def _git(a, *args):
            try:
                return run_git(a, *args)
            except subprocess.TimeoutExpired:
                return subprocess.CompletedProcess(args, 124, "", f"git timed out after {T}s")
    """)
    assert timeout_constructions(src) == [6]


def test_guard_b_ignores_the_marker_row_and_unrelated_results() -> None:
    """The conjunction is what discriminates: neither the marker table entry nor an
    ordinary rc-124 result is a construction of the synthetic timeout."""
    assert timeout_constructions('MARKERS = ("git timed out after",)\n') == []
    assert timeout_constructions('r = subprocess.CompletedProcess(a, 124, "", "killed")\n') == []
    assert (
        timeout_constructions('r = subprocess.CompletedProcess(a, 1, "", "timed out after 3s")\n')
        == []
    )
