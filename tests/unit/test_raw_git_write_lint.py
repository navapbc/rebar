"""Raw-git-write lint (ticket d37e-4f64-3265-4f30).

Two-layer deterministic lint over rebar's store write surface:

- Layer P (Python, AST): raw subprocess git mutations (R1) and mutation verbs
  reaching the shared git wrappers by name (R2), with local intra-function
  argv tracking and a fail-closed opaque-argv class.
- Layer W (workflows + shell, cwd-aware within-step): git mutation verbs in a
  step/script block that establishes tracker context (.tickets-tracker).

Sanction is a single inline marker with a mandatory reason:
``# raw-git-ok: <reason>``. These tests drive the lint against synthetic
trees under tmp_path; the fixtures here deliberately contain raw git write
shapes, so this module is the lint's EXCLUDED_FILES fixture corpus.
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_raw_git_writes.py"


@pytest.fixture(scope="module")
def lint():
    spec = importlib.util.spec_from_file_location("check_raw_git_writes", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_raw_git_writes"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    (tmp_path / "src" / "rebar").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    return tmp_path


def _py(tree: Path, rel: str, body: str) -> Path:
    p = tree / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _wf(tree: Path, name: str, body: str) -> Path:
    p = tree / ".github" / "workflows" / name
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def _kinds(violations) -> list[str]:
    return [v.kind for v in violations]


# ---------------------------------------------------------------------------
# Layer P — R1: raw subprocess git mutations
# ---------------------------------------------------------------------------


def test_raw_subprocess_git_commit_unmarked_fails(lint, tree):
    _py(
        tree,
        "src/rebar/bypass.py",
        """
        import subprocess

        def sneak(t):
            subprocess.run(["git", "-C", str(t), "commit", "-m", "x"])
        """,
    )
    violations = lint.check(tree)
    assert len(violations) == 1
    assert violations[0].kind == "raw-git-mutation"


def test_marker_with_reason_passes(lint, tree):
    _py(
        tree,
        "src/rebar/seam.py",
        """
        import subprocess

        def seam(t):
            subprocess.run(["git", "-C", str(t), "add", "-A"])  # raw-git-ok: seam internal
        """,
    )
    assert lint.check(tree) == []


def test_marker_without_reason_fails(lint, tree):
    _py(
        tree,
        "src/rebar/seam.py",
        """
        import subprocess

        def seam(t):
            subprocess.run(["git", "-C", str(t), "add", "-A"])  # raw-git-ok:
        """,
    )
    kinds = _kinds(lint.check(tree))
    assert "marker-without-reason" in kinds


def test_git_read_verbs_never_fire(lint, tree):
    _py(
        tree,
        "src/rebar/reader.py",
        """
        import subprocess

        def peek(t):
            subprocess.run(["git", "-C", str(t), "status", "--porcelain"])
            subprocess.run(["git", "log", "--oneline"])
            subprocess.check_output(["git", "rev-parse", "HEAD"])
        """,
    )
    assert lint.check(tree) == []


def test_remote_add_and_worktree_add_do_not_fire(lint, tree):
    _py(
        tree,
        "src/rebar/plumbing.py",
        """
        import subprocess

        def wire(url, wt):
            subprocess.run(["git", "remote", "add", "origin", url])
            subprocess.run(["git", "worktree", "add", wt, "origin/main"])
        """,
    )
    assert lint.check(tree) == []


def test_global_options_skipped_before_subcommand(lint, tree):
    _py(
        tree,
        "src/rebar/opts.py",
        """
        import subprocess

        def surgery(t):
            subprocess.run(["git", "-C", str(t), "-c", "user.name=bot", "add", "-A"])
        """,
    )
    assert _kinds(lint.check(tree)) == ["raw-git-mutation"]


def test_opaque_raw_subprocess_fires_fail_closed(lint, tree):
    _py(
        tree,
        "src/rebar/runnermod.py",
        """
        import subprocess

        def runner(argv):
            return subprocess.run(argv, capture_output=True)
        """,
    )
    kinds = _kinds(lint.check(tree))
    assert kinds == ["unresolvable-argv"]


def test_opaque_raw_subprocess_marker_silences(lint, tree):
    _py(
        tree,
        "src/rebar/runnermod.py",
        """
        import subprocess

        def runner(argv):
            return subprocess.run(argv, capture_output=True)  # raw-git-ok: generic runner
        """,
    )
    assert lint.check(tree) == []


def test_non_git_subprocess_never_fires(lint, tree):
    _py(
        tree,
        "src/rebar/other.py",
        """
        import subprocess

        def probe():
            subprocess.run(["curl", "--fail", "https://example.com"])
            subprocess.run(["rebar", "create", "bug", "t"])
        """,
    )
    assert lint.check(tree) == []


def test_local_list_argv_to_subprocess_fires(lint, tree):
    _py(
        tree,
        "src/rebar/builder.py",
        """
        import subprocess

        def build_and_run(t, msg):
            argv = ["git", "-C", str(t), "commit"]
            argv.append("-m")
            argv.append(msg)
            subprocess.run(argv)
        """,
    )
    assert _kinds(lint.check(tree)) == ["raw-git-mutation"]


# ---------------------------------------------------------------------------
# Layer P — R2: wrapper-name calls
# ---------------------------------------------------------------------------


def test_wrapper_literal_mutation_verb_fires(lint, tree):
    _py(
        tree,
        "src/rebar/wrap1.py",
        """
        def txn(root, run_git_write):
            run_git_write(root, "add", "--all")
        """,
    )
    assert _kinds(lint.check(tree)) == ["wrapper-git-mutation"]


def test_wrapper_nested_list_literal_fires(lint, tree):
    _py(
        tree,
        "src/rebar/wrap2.py",
        """
        def cas(ref, new, _git):
            _git(["update-ref", ref, new])
        """,
    )
    assert _kinds(lint.check(tree)) == ["wrapper-git-mutation"]


def test_wrapper_splatted_local_list_fires(lint, tree):
    _py(
        tree,
        "src/rebar/wrap3.py",
        """
        from rebar._store.gitutil import run_git

        def commit(repo_root, message):
            args = ["commit", "--quiet"]
            args.append("-m")
            args.append(message)
            return run_git(repo_root, *args)
        """,
    )
    assert _kinds(lint.check(tree)) == ["wrapper-git-mutation"]


def test_wrapper_plain_local_list_argv_fires(lint, tree):
    _py(
        tree,
        "src/rebar/wrap4.py",
        """
        def _commit_paths(tracker, _run_git):
            argv = ["git", "-C", str(tracker), "commit", "--quiet"]
            _run_git(argv)
        """,
    )
    assert _kinds(lint.check(tree)) == ["wrapper-git-mutation"]


def test_wrapper_local_list_read_verb_does_not_fire(lint, tree):
    _py(
        tree,
        "src/rebar/wrap5.py",
        """
        from rebar._store.gitutil import run_git

        def history(repo_root):
            args = ["log", "--oneline"]
            args.append("--max-count=5")
            return run_git(repo_root, *args)
        """,
    )
    assert lint.check(tree) == []


def test_wrapper_read_literals_with_scalar_variable_pass(lint, tree):
    _py(
        tree,
        "src/rebar/wrap6.py",
        """
        from rebar._store.gitutil import run_git

        def resolve(repo_root, branch):
            return run_git(repo_root, "rev-parse", branch)
        """,
    )
    assert lint.check(tree) == []


def test_wrapper_opaque_star_forwarding_fires_and_marker_silences(lint, tree):
    _py(
        tree,
        "src/rebar/wrap7.py",
        """
        from rebar._store.gitutil import run_git

        def _git(tracker, *args):
            return run_git(tracker, *args)
        """,
    )
    assert _kinds(lint.check(tree)) == ["unresolvable-argv-wrapper"]

    _py(
        tree,
        "src/rebar/wrap7.py",
        """
        from rebar._store.gitutil import run_git

        # raw-git-ok: delegation seam, callers are linted by wrapper name
        def _git(tracker, *args):
            return run_git(tracker, *args)
        """,
    )
    assert lint.check(tree) == []


def test_maintenance_caller_fires_by_name_and_function_marker_silences(lint, tree):
    _py(
        tree,
        "src/rebar/maint.py",
        """
        def compact(tracker, _git):
            _git(tracker, "add", "-A")
            _git(tracker, "commit", "-m", "compact")
        """,
    )
    assert _kinds(lint.check(tree)) == ["wrapper-git-mutation", "wrapper-git-mutation"]

    _py(
        tree,
        "src/rebar/maint.py",
        """
        # raw-git-ok: store-maintenance command, seam-internal
        def compact(tracker, _git):
            _git(tracker, "add", "-A")
            _git(tracker, "commit", "-m", "compact")
        """,
    )
    assert lint.check(tree) == []


def test_sibling_wrapper_names_not_in_name_set(lint, tree):
    _py(
        tree,
        "src/rebar/wrap8.py",
        """
        def sync(root, _git_ok, _git_push):
            _git_ok(root, "add", "-A")
            _git_push(root, "origin", "main")
        """,
    )
    assert lint.check(tree) == []


# ---------------------------------------------------------------------------
# Layer P — R2: the ATTRIBUTE call form (bug 4073-9d4f-c644-44a9)
#
# R2 matches the wrapper by the CALLABLE's name, which for an attribute call is
# the attribute — so `core._git(...)` is linted exactly like a bare `_git(...)`.
# That is load-bearing for the late-binding idiom in
# `rebar_reconciler/_ref_lock_push.py`, where the caller's `_ref_lock` module is
# passed down as `core` so `monkeypatch.setattr(_ref_lock, "_git", ...)` keeps
# working across the module boundary. Every other R2 test above uses the bare
# form; these pin the attribute form so the coverage cannot be read as absent.
# ---------------------------------------------------------------------------


def test_wrapper_attribute_form_mutation_verb_fires(lint, tree):
    _py(
        tree,
        "src/rebar/attr1.py",
        """
        def compact(core, repo_root):
            core._git(repo_root, ["commit", "-m", "compact"])
            core.run_git(repo_root, ["add", "-A"])
        """,
    )
    assert _kinds(lint.check(tree)) == ["wrapper-git-mutation", "wrapper-git-mutation"]


def test_wrapper_attribute_receiver_shape_does_not_matter(lint, tree):
    """`self.`, a nested receiver, and an imported module all resolve alike."""
    _py(
        tree,
        "src/rebar/attr2.py",
        """
        from rebar._engine.rebar_reconciler import _ref_lock

        class Store:
            def compact(self, repo_root):
                self._git(repo_root, ["commit", "-m", "x"])

        def nested(core, repo_root):
            core.inner._git(repo_root, ["update-ref", "refs/x", "0" * 40])

        def imported(repo_root):
            _ref_lock._git(repo_root, ["reset", "--hard"])
        """,
    )
    assert _kinds(lint.check(tree)) == [
        "wrapper-git-mutation",
        "wrapper-git-mutation",
        "wrapper-git-mutation",
    ]


def test_wrapper_attribute_form_opaque_argv_fires_and_marker_silences(lint, tree):
    _py(
        tree,
        "src/rebar/attr3.py",
        """
        def delegate(core, repo_root, *args):
            return core._git(repo_root, *args)
        """,
    )
    assert _kinds(lint.check(tree)) == ["unresolvable-argv-wrapper"]

    _py(
        tree,
        "src/rebar/attr3.py",
        """
        def delegate(core, repo_root, *args):
            return core._git(repo_root, *args)  # raw-git-ok: seam-internal delegation
        """,
    )
    assert lint.check(tree) == []


def test_attribute_form_push_lease_cas_shape_does_not_fire(lint, tree):
    """The real `_ref_lock_push.push_lease_cas` shape stays clean — on the VERB.

    Its `core._git(...)` call IS seen and its argv IS resolved; it simply carries
    `push`, which is not a mutation verb. Pinned so the absence of a
    `# raw-git-ok:` marker on that module reads as a resolved disposition rather
    than as evidence the attribute form escapes the gate.
    """
    _py(
        tree,
        "src/rebar/attr4.py",
        """
        def push_lease_cas(core, repo_root, ref, old_oid, remote, refspec):
            return core._git(
                repo_root,
                ["push", f"--force-with-lease={ref}:{old_oid}", remote, refspec],
                timeout=core._REMOTE_TIMEOUT_SECS,
                check=False,
            )

        def read(core, repo_root):
            return core._git(repo_root, ["rev-parse", "HEAD"])
        """,
    )
    assert lint.check(tree) == []


# ---------------------------------------------------------------------------
# Layer W — workflows + shell
# ---------------------------------------------------------------------------


def test_workflow_cd_tracker_bare_git_add_fails(lint, tree):
    """The reconcile-bridge.yml historical bypass shape (AC fixture)."""
    _wf(
        tree,
        "bridge.yml",
        """
        jobs:
          sync:
            runs-on: ubuntu-latest
            steps:
              - name: commit back
                run: |
                  cd .tickets-tracker
                  git add -A
                  git commit -m "reconcile"
        """,
    )
    kinds = _kinds(lint.check(tree))
    assert kinds == ["workflow-tracker-git-mutation"]


def test_workflow_git_dash_c_tracker_fails(lint, tree):
    _wf(
        tree,
        "surgery.yml",
        """
        jobs:
          fix:
            runs-on: ubuntu-latest
            steps:
              - name: inline surgery
                run: git -C .tickets-tracker commit -m "x"
        """,
    )
    assert _kinds(lint.check(tree)) == ["workflow-tracker-git-mutation"]


def test_workflow_bare_git_commit_without_tracker_context_passes(lint, tree):
    """Docs/artifact commits in workflows never fire (no tracker context)."""
    _wf(
        tree,
        "docs.yml",
        """
        jobs:
          docs:
            runs-on: ubuntu-latest
            steps:
              - name: publish docs
                run: |
                  git add docs/
                  git commit -m "docs"
                  git push origin main
        """,
    )
    assert lint.check(tree) == []


def test_workflow_working_directory_tracker_fails(lint, tree):
    _wf(
        tree,
        "wd.yml",
        """
        jobs:
          sync:
            runs-on: ubuntu-latest
            steps:
              - name: commit inside tracker
                working-directory: .tickets-tracker
                run: git add -A
        """,
    )
    assert _kinds(lint.check(tree)) == ["workflow-tracker-git-mutation"]


def test_workflow_marker_comment_silences_step(lint, tree):
    _wf(
        tree,
        "bridge.yml",
        """
        jobs:
          sync:
            runs-on: ubuntu-latest
            steps:
              - name: commit back CAS loop
                run: |
                  # raw-git-ok: fixture proves an explicitly reviewed workflow exemption
                  cd .tickets-tracker
                  git add -A
                  git commit -m "reconcile"
        """,
    )
    assert lint.check(tree) == []


def test_workflow_marker_without_reason_fails(lint, tree):
    _wf(
        tree,
        "bridge.yml",
        """
        jobs:
          sync:
            runs-on: ubuntu-latest
            steps:
              - name: commit back
                run: |
                  # raw-git-ok:
                  cd .tickets-tracker
                  git add -A
        """,
    )
    kinds = _kinds(lint.check(tree))
    assert "marker-without-reason" in kinds


def test_shell_script_tracker_context_fails(lint, tree):
    p = tree / "scripts" / "sync.sh"
    p.write_text(
        textwrap.dedent(
            """
            #!/bin/sh
            cd .tickets-tracker
            git add -A
            git commit -m "sync"
            """
        ),
        encoding="utf-8",
    )
    assert _kinds(lint.check(tree)) == ["workflow-tracker-git-mutation"]


def test_shell_script_no_tracker_context_passes(lint, tree):
    p = tree / "scripts" / "release.sh"
    p.write_text(
        textwrap.dedent(
            """
            #!/bin/sh
            git add CHANGELOG.md
            git commit -m "release notes"
            """
        ),
        encoding="utf-8",
    )
    assert lint.check(tree) == []


# ---------------------------------------------------------------------------
# teaching message, report mode, self-exclusion
# ---------------------------------------------------------------------------


def test_enforce_mode_prints_teaching_message(lint, tree, capsys):
    _py(
        tree,
        "src/rebar/bypass.py",
        """
        import subprocess

        def sneak(t):
            subprocess.run(["git", "-C", str(t), "commit", "-m", "x"])
        """,
    )
    rc = lint.main(["--root", str(tree)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "event_append" in out or "event-append" in out
    assert "git_adapter" in out
    assert "raw-git-ok" in out


def test_report_mode_exits_zero_and_lists_hits(lint, tree, capsys):
    _py(
        tree,
        "src/rebar/bypass.py",
        """
        import subprocess

        def sneak(t):
            subprocess.run(["git", "-C", str(t), "commit", "-m", "x"])
        """,
    )
    _py(
        tree,
        "src/rebar/seam.py",
        """
        import subprocess

        def seam(t):
            subprocess.run(["git", "-C", str(t), "add", "-A"])  # raw-git-ok: seam internal
        """,
    )
    rc = lint.main(["--root", str(tree), "--report"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "bypass.py" in out
    assert "seam.py" in out  # marked hits appear in the inventory with their reason
    assert "seam internal" in out


def test_clean_tree_exits_zero(lint, tree):
    _py(tree, "src/rebar/pure.py", "def f():\n    return 1\n")
    assert lint.main(["--root", str(tree)]) == 0


# ---------------------------------------------------------------------------
# Layer P — R3: seam composition (ticket 638a-3746-e58a-4929)
#
# R1/R2 enforce the MECHANISM rule ("no raw subprocess git"). R3 enforces the
# PROTOCOL rule: the git-adapter's mutation primitives are sanctioned exports,
# so a caller could compose them into a hand-rolled non-atomic sequence and
# every individual call would pass. R3 fires on such a composition made from
# outside the reconciler write-seam files.
# ---------------------------------------------------------------------------


def test_r3_composed_add_commit_outside_seam_fires(lint, tree):
    _py(
        tree,
        "src/rebar/compose.py",
        """
        from rebar_reconciler import git_adapter

        def snapshot(d):
            git_adapter.add(d, "bindings.json")
            git_adapter.commit(d, "persist", no_verify=True)
        """,
    )
    violations = lint.check(tree)
    assert _kinds(violations) == ["seam-composition", "seam-composition"]
    assert "add" in violations[0].detail
    assert "commit" in violations[1].detail


def test_r3_detached_index_purge_shape_fires(lint, tree):
    _py(
        tree,
        "src/rebar/purge.py",
        """
        from rebar_reconciler import git_adapter

        def purge(root, present, old, env):
            git_adapter.read_tree(root, old, env=env)
            git_adapter.rm_cached(root, *present, env=env)
            t = git_adapter.write_tree(root, env=env)
            c = git_adapter.commit_tree(root, t, parent=old, message="m", env=env)
            git_adapter.update_ref(root, "refs/heads/tickets", c, old)
        """,
    )
    assert _kinds(lint.check(tree)) == ["seam-composition"] * 5


@pytest.mark.parametrize("seam_file", ["git_adapter.py", "_ref_lock.py", "_ref_lock_push.py"])
def test_r3_does_not_fire_inside_the_write_seam(lint, tree, seam_file):
    _py(
        tree,
        f"src/rebar/{seam_file}",
        """
        from rebar_reconciler import git_adapter

        def under_the_lock(d):
            git_adapter.add(d, "bindings.json")
            git_adapter.commit(d, "persist")
        """,
    )
    assert lint.check(tree) == []


def test_r3_line_marker_with_reason_silences(lint, tree):
    _py(
        tree,
        "src/rebar/compose.py",
        """
        from rebar_reconciler import git_adapter

        def snapshot(d):
            git_adapter.add(d, "b.json")  # raw-git-ok: selective staging, see 11a9
            git_adapter.commit(d, "m")  # raw-git-ok: selective staging, see 11a9
        """,
    )
    assert lint.check(tree) == []


def test_r3_function_marker_with_reason_silences(lint, tree):
    _py(
        tree,
        "src/rebar/compose.py",
        """
        from rebar_reconciler import git_adapter

        # raw-git-ok: detached temp index + CAS ref-advance; never touches the worktree
        def purge(root, old, env):
            git_adapter.read_tree(root, old, env=env)
            git_adapter.update_ref(root, "refs/heads/tickets", "new", old)
        """,
    )
    assert lint.check(tree) == []


def test_r3_marker_without_reason_fails(lint, tree):
    _py(
        tree,
        "src/rebar/compose.py",
        """
        from rebar_reconciler import git_adapter

        def snapshot(d):
            git_adapter.commit(d, "m")  # raw-git-ok:
        """,
    )
    kinds = _kinds(lint.check(tree))
    assert "marker-without-reason" in kinds
    assert "seam-composition" in kinds


def test_r3_ignores_non_git_adapter_add_and_commit(lint, tree):
    _py(
        tree,
        "src/rebar/ordinary.py",
        """
        def collect(session, items):
            seen = set()
            for i in items:
                seen.add(i)
            session.commit()
            session.add(items)
            return seen
        """,
    )
    assert lint.check(tree) == []


def test_r3_ignores_git_adapter_read_ops(lint, tree):
    _py(
        tree,
        "src/rebar/reads.py",
        """
        from rebar_reconciler import git_adapter

        def probe(d, sha):
            git_adapter.rev_parse(d, "tickets")
            git_adapter.cat_file_exists(d, "tickets:f")
            git_adapter.diff_cached_names(d)
            git_adapter.commit_email(d, sha)
            git_adapter.verify_commit(d, sha)
        """,
    )
    assert lint.check(tree) == []


def test_r3_dynamic_loader_binding_fires(lint, tree):
    _py(
        tree,
        "src/rebar/lazy.py",
        """
        def snapshot(d):
            git_adapter = _load("rebar_reconciler.git_adapter", "git_adapter.py")
            git_adapter.commit(d, "m")
        """,
    )
    assert _kinds(lint.check(tree)) == ["seam-composition"]


def test_r3_aliased_import_binding_fires(lint, tree):
    _py(
        tree,
        "src/rebar/aliased.py",
        """
        from rebar_reconciler import git_adapter as ga

        def snapshot(d):
            ga.add(d, "b.json")
        """,
    )
    assert _kinds(lint.check(tree)) == ["seam-composition"]


def test_r3_from_imported_primitive_bare_name_fires(lint, tree):
    _py(
        tree,
        "src/rebar/direct.py",
        """
        from rebar_reconciler.git_adapter import add, commit, rev_parse

        def snapshot(d):
            rev_parse(d, "tickets")
            add(d, "b.json")
            commit(d, "m")
        """,
    )
    assert _kinds(lint.check(tree)) == ["seam-composition", "seam-composition"]


def test_r3_report_mode_lists_seam_composition_hits(lint, tree, capsys):
    _py(
        tree,
        "src/rebar/compose.py",
        """
        from rebar_reconciler import git_adapter

        def snapshot(d):
            git_adapter.commit(d, "m")  # raw-git-ok: durable reason here
        """,
    )
    rc = lint.main(["--root", str(tree), "--report"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "seam-composition" in out
    assert "durable reason here" in out


def test_repo_tree_has_no_unsanctioned_raw_git_writes(lint):
    """The gate must pass on rebar's own tree — the in-process equivalent of the
    CI step, so the policy holds in environments with no CI provider."""
    assert lint.check(REPO_ROOT) == []
