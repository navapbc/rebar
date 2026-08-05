"""``search_files`` must never answer "(no matches)" for a path it did not search (bug bf31).

``search_files``'s whole body is ``for dirpath, dirs, files in os.walk(base)``. ``os.walk``
on a **file** — or on a path that does not exist — yields nothing, so control falls straight
through to ``return "\\n".join(hits) or "(no matches)"``. The agent is told the literal is
absent when it was never looked for.

This is not cosmetic. It was MEASURED as the engine of a non-convergent verifier loop while
closing epic ``e369-a449-4773-48fb``: an instrumented run (264 tool calls, 1074s,
``max_iterations=480``) spent calls #121-264 in a closed 4-call cycle, 0% of it novel, because
``search_files("J1", "tests/unit/rebar_reconciler/test_backend_characterization.py")`` kept
answering "(no matches)" for a literal that sits on line 116 of that very file. Across the run
**48% of ``search_files`` calls passed a file path, 100% of those returned "(no matches)", and
26 of 29 had the literal genuinely present.**

It is also a **false-FAIL vector on a signed gate**: the packaged ``completion-verifier`` prompt
tells the agent that "a ``(no matches)`` result means only that *that literal string* is absent"
and pushes it to narrow the search path — the exact move that returns the lie. A completion
verdict is signed, so a false NOT MET is an invisible signed false FAIL.

The contract these tests pin: **"(no matches)" means the literal was searched for and is
absent — nothing else.** A path that cannot be searched must say so.

The DIRECTORY branch is the one every existing gate already depends on, so it is pinned here as
an explicit regression, not merely assumed.
"""

from __future__ import annotations

import pytest

from rebar.llm import pai_tools

pytestmark = pytest.mark.unit


def _search(root):
    """The real tool, built exactly as the runner builds it (pai_tools.py:341-349)."""
    _read_file, _list_directory, search_files = pai_tools.filesystem_tools(str(root))
    return search_files


@pytest.fixture
def tree(tmp_path):
    """A small repo-shaped tree.

    Deliberately several files with DIFFERENT names, extensions and literals so no assertion
    can pass by accident off one hardcoded fixture field — the query, the file and the
    expected line all vary across the cases below.
    """
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "alpha.py").write_text(
        "import os\nMARKER_ALPHA = 1\ndef alpha():\n    return MARKER_ALPHA\n",
        encoding="utf-8",
    )
    (pkg / "beta.py").write_text(
        "# beta module\nBETA_TOKEN = 'zulu'\n",
        encoding="utf-8",
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "guide.md").write_text(
        "# Guide\n\nSet `allow_insecure = true` to permit http.\n",
        encoding="utf-8",
    )
    return tmp_path


# ── The bug: a FILE path is never searched, and the lie is indistinguishable from absence ──
@pytest.mark.parametrize(
    ("relpath", "query", "expect_line"),
    [
        ("pkg/alpha.py", "MARKER_ALPHA", 2),  # a symbol, first occurrence on line 2
        ("pkg/beta.py", "BETA_TOKEN", 2),  # a different file, different literal
        ("docs/guide.md", "allow_insecure", 3),  # a non-.py file, a config key
    ],
)
def test_file_path_containing_the_literal_returns_the_hit(tree, relpath, query, expect_line):
    """`path` naming a FILE that CONTAINS `query` must return the hit, not "(no matches)".

    This is the exact shape that looped the verifier: narrowing the search to the one file the
    evidence lives in is the natural move, and today it is answered with a falsehood.
    """
    result = _search(tree)(query, relpath)

    assert result != "(no matches)", (
        f"search_files({query!r}, {relpath!r}) claimed the literal is absent, but it is "
        f"present in that file — a false negative, not an empty result"
    )
    assert f"{relpath}:{expect_line}:" in result, (
        f"expected a '{relpath}:{expect_line}: ...' hit line; got: {result!r}"
    )
    assert query in result


# ── Honest absence must survive: a file that really lacks the literal still says so ──
def test_file_path_without_the_literal_still_reports_no_matches(tree):
    """The fix must not manufacture hits. A FILE genuinely lacking the literal keeps the
    honest "(no matches)" answer — that is what makes the value meaningful again."""
    assert _search(tree)("MARKER_ALPHA", "pkg/beta.py") == "(no matches)"


# ── Regression pin: the DIRECTORY branch is what every gate already depends on ──
def test_directory_path_behaviour_is_unchanged(tree):
    """`path` naming a DIRECTORY must keep working exactly as before — same `file:line: text`
    shape, and recursion into subdirectories. Pinned explicitly because plan-review,
    code-review and completion all call this tool through the directory branch."""
    result = _search(tree)("MARKER_ALPHA", "pkg")
    assert "pkg/alpha.py:2:" in result
    assert "MARKER_ALPHA" in result

    # recursion from the root still reaches nested files
    root_result = _search(tree)("allow_insecure", ".")
    assert "docs/guide.md:3:" in root_result

    # and a directory genuinely lacking the literal still reports absence
    assert _search(tree)("MARKER_ALPHA", "docs") == "(no matches)"


# ── A path that does not exist is the same lie in different clothes ──
@pytest.mark.parametrize(
    "missing",
    ["pkg/nope.py", "does/not/exist", "pkg/alpha.pyc"],
)
def test_nonexistent_path_is_an_explicit_error_not_no_matches(tree, missing):
    """A mistyped path must be distinguishable from an honest absence.

    Returning "(no matches)" here tells the agent "the literal is absent" when the truth is
    "you named a path I could not search" — which is precisely how a verifier concludes NOT MET
    against code that exists.
    """
    result = _search(tree)("MARKER_ALPHA", missing)

    assert result != "(no matches)", (
        f"a nonexistent path ({missing!r}) must not be reported as an honest absence"
    )
    assert result.startswith("Error:"), f"expected an explicit tool error; got: {result!r}"


# ── An explicitly named file must not be hidden by the DISCOVERY filter ──
def test_gitignored_file_named_explicitly_is_still_searched(tmp_path):
    """The nastiest way to re-introduce this bug: apply the DISCOVERY filter to the file branch.

    ``_discovery_filter``'s ``skip_file`` (``fs_tools.py:124-127``) is
    ``tracked is not None and abs_path not in tracked``, where ``_git_tracked``
    (``fs_tools.py:95-113``) is ``git ls-files --cached --others --exclude-standard`` — i.e. it
    hides **gitignored** files. That filter exists so the agent is not drowned while *discovering*
    files. Applying it to a path the agent named EXPLICITLY would answer "(no matches)" for a file
    that plainly contains the literal — this bug's exact lie, wearing a different hat.
    ``read_file`` already settled the precedent (``fs_tools.py:42-43``): "an explicitly named file
    is always readable (only the security deny-list blocks it)".

    The other tests here cannot catch this: their ``tmp_path`` root is not a git repo, so
    ``_git_tracked`` returns ``None`` and ``skip_file`` never fires. This one builds a real repo
    AND asserts the filter is genuinely armed before trusting the result — an unarmed fixture would
    make the assertion below unfalsifiable.
    """
    import subprocess

    def run(*args):
        subprocess.run(args, cwd=tmp_path, capture_output=True, check=True)

    run("git", "init", "-q")
    (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
    (tmp_path / "tracked.py").write_text("SHARED_LITERAL = 'tracked'\n", encoding="utf-8")
    run("git", "add", ".gitignore", "tracked.py")

    build = tmp_path / "build"
    build.mkdir()
    (build / "generated.py").write_text("SHARED_LITERAL = 'generated'\n", encoding="utf-8")

    search_files = _search(tmp_path)

    # The fixture must actually ARM the filter, or the real assertion below proves nothing:
    # discovery from the root must see the tracked file and HIDE the gitignored one.
    discovered = search_files("SHARED_LITERAL", ".")
    assert "tracked.py:1:" in discovered
    assert "build/generated.py" not in discovered, (
        "fixture is not armed: the discovery filter did not hide the gitignored file, so this "
        "test could not detect the filter being wrongly applied to an explicit file path"
    )

    # ...and yet naming that file EXPLICITLY must still search it.
    result = search_files("SHARED_LITERAL", "build/generated.py")
    assert result != "(no matches)", (
        "an explicitly named, existing file containing the literal was reported absent because "
        "it is gitignored — the discovery filter must not apply to an explicitly named file"
    )
    assert "build/generated.py:1:" in result


# ── The safety envelope must not weaken for the new file branch ──
def test_file_path_outside_the_root_is_still_refused(tree, tmp_path):
    """Repo-root confinement is enforced by `_safe_path` and must apply to a FILE path exactly
    as it does to a directory — the new branch must not become an escape hatch."""
    outside = tmp_path.parent / "outside_secret.py"
    outside.write_text("MARKER_ALPHA = 'leaked'\n", encoding="utf-8")

    result = _search(tree)("MARKER_ALPHA", f"../{outside.name}")

    assert "MARKER_ALPHA = 'leaked'" not in result
    assert result.startswith("Error:"), f"expected refusal; got: {result!r}"
