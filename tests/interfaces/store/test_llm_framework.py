"""Tests for the rebar.llm agent-operations framework + the review_ticket op.

All offline: the agent run is exercised through a FakeRunner (the dependency-
injection seam), so no model, network, or `agents` extra is needed. The live
pydantic_ai path is tested only for its graceful-degradation errors.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import rebar
from rebar import schemas


# ── import-cleanliness (the hard optionality rule) ────────────────────────────
def test_import_rebar_llm_pulls_no_heavy_deps() -> None:
    """`import rebar.llm` must not import the agent runtime (pydantic_ai) / langfuse /
    anthropic — they are lazy. Run in a clean subprocess so import order can't mask it."""
    code = (
        "import sys, rebar.llm;"
        "heavy=[m for m in "
        "('pydantic_ai','langfuse','anthropic') if m in sys.modules];"
        "print('HEAVY' if heavy else 'CLEAN', heavy)"
    )
    cp = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": "src", "PATH": __import__("os").environ.get("PATH", "")},
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.startswith("CLEAN"), cp.stdout


# ── reviewer registry + prompt resolution (offline fallback) ──────────────────
def test_catalog_and_fallback_prompt_render() -> None:
    import rebar.llm as llm
    from rebar.llm import prompts

    catalog = llm.load_catalog()
    assert "ticket-quality" in catalog
    assert catalog["ticket-quality"].default is True
    rv = llm.get_reviewer("ticket-quality")
    text, meta = prompts.resolve_prompt(
        rv, {"ticket_id": "T1", "ticket_context": "CTX", "repo_path": "/x"}, None
    )
    assert "T1" in text and "CTX" in text
    # WS-F1: git-canonical — meta carries the content hash + provenance (no Langfuse
    # object; Langfuse is never consulted for the text).
    assert meta["source"] == "git"
    assert len(meta["content_sha256"]) == 64
    assert meta["prompt_id"] == "ticket-quality"


def test_unknown_reviewer_raises() -> None:
    import rebar.llm as llm
    from rebar.llm.prompts import ReviewerError

    with pytest.raises(ReviewerError):
        llm.get_reviewer("does-not-exist")


# ── deterministic reviewer selection (the rules layer) ────────────────────────
@pytest.mark.parametrize(
    "changed, expected",
    [
        ([], {"ticket-quality"}),
        (["src/rebar/auth_helpers.py"], {"ticket-quality", "security"}),
        (["src/rebar/signing.py"], {"ticket-quality", "security"}),
        (["tests/test_x.py"], {"ticket-quality", "tests"}),
        (["src/rebar/auth.py", "tests/test_auth.py"], {"ticket-quality", "security", "tests"}),
        (["README.md"], {"ticket-quality"}),
    ],
)
def test_select_reviewers_rules(changed, expected) -> None:
    import rebar.llm as llm

    assert set(llm.select_reviewers(changed)) == expected


# ── findings normalization / citation resolution / validation ─────────────────
def test_normalize_coerces_shape() -> None:
    from rebar.llm.findings import normalize_finding

    f = normalize_finding({"severity": "BOGUS", "category": "x", "description": "d"})
    assert f["severity"] == "info"  # unknown clamps to info
    assert f["dimension"] == "x" and f["detail"] == "d"
    assert f["citations"] == []


def test_normalize_strips_model_emitted_nulls() -> None:
    """A real model may emit explicit nulls on optional string fields (title) or
    citation fields (path/url on a source citation) — those are typed `string` in
    the schema, so a None would fail validation. They must be stripped (None ==
    absent) so one sloppy field can't sink an otherwise-valid review. Regression
    for a live-run FindingsError ('None is not of type string')."""
    from rebar.llm.findings import build_result, normalize_finding, validate_result

    f = normalize_finding(
        {
            "severity": "high",
            "dimension": "security",
            "detail": "hardcoded secret",
            "title": None,  # model emitted an explicit null
            "citations": [{"kind": "source", "description": "evidence", "path": None, "url": None}],
        }
    )
    assert "title" not in f
    assert "path" not in f["citations"][0] and "url" not in f["citations"][0]
    # End-to-end: the assembled review_result now validates cleanly.
    validate_result(build_result([f], runner="fake"))


def test_resolve_citations_downgrades_unresolved(tmp_path: Path) -> None:
    from rebar.llm.findings import build_result, resolve_citations

    (tmp_path / "real.py").write_text("a\nb\nc\n", encoding="utf-8")
    result = build_result(
        [
            {
                "severity": "high",
                "dimension": "d",
                "detail": "x",
                "citations": [
                    {"kind": "file", "path": "real.py", "line_start": 1, "line_end": 2},
                    {"kind": "file", "path": "real.py", "line_start": 99},  # out of range
                    {"kind": "file", "path": "missing.py", "line_start": 1},  # no such file
                    "freeform",
                ],
            }
        ],
        runner="fake",
    )
    resolve_citations(result, str(tmp_path))
    kinds = [c["kind"] for c in result["findings"][0]["citations"]]
    # valid file kept; out-of-range + missing downgraded to source; freeform = source
    assert kinds == ["file", "source", "source", "source"]


def test_resolve_citations_rejects_denied_state_paths(tmp_path: Path) -> None:
    """A citation into .git/.tickets-tracker/.bridge_state must be downgraded — the
    file-tool sandbox guarantee has to hold in the OUTPUT too (PR #6 review)."""
    from rebar.llm.findings import build_result, resolve_citations

    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (tmp_path / ".bridge_state").mkdir()
    (tmp_path / ".bridge_state" / "map.json").write_text("{}\n", encoding="utf-8")
    result = build_result(
        [
            {
                "severity": "high",
                "dimension": "d",
                "detail": "x",
                "citations": [
                    {"kind": "file", "path": ".git/config", "line_start": 1},
                    {"kind": "file", "path": ".bridge_state/map.json", "line_start": 1},
                ],
            }
        ],
        runner="fake",
    )
    resolve_citations(result, str(tmp_path))
    kinds = [c["kind"] for c in result["findings"][0]["citations"]]
    assert kinds == ["source", "source"]  # both denied -> downgraded


def _fs_tools(root):
    """The pydantic_ai file tools (plain functions) keyed by name."""
    pytest.importorskip("pydantic_ai")
    from rebar.llm import pai_tools

    return {t.__name__: t for t in pai_tools.filesystem_tools(str(root))}


def test_read_file_tool_windows_a_large_file(tmp_path: Path) -> None:
    """read_file returns at most _READ_MAX_LINES of a large file (a bounded window),
    not the whole thing, and a narrow range returns exactly that range."""
    from rebar.llm.fs_tools import _SCAN_MAX_FILES  # noqa: F401  (import-light sanity)
    from rebar.llm.pai_tools import _READ_MAX_LINES

    big = tmp_path / "big.txt"
    big.write_text(
        "".join(f"line {i}\n" for i in range(1, _READ_MAX_LINES + 501)), encoding="utf-8"
    )
    read_file = _fs_tools(tmp_path)["read_file"]
    out = read_file("big.txt")
    assert out.count("\n") <= _READ_MAX_LINES  # capped window, not the full file
    # a narrow range returns exactly that window (tab-delimited `lineno\ttext`)
    narrow = read_file("big.txt", line_start=5, line_end=7)
    assert narrow.startswith("5\tline 5")
    assert narrow.strip().endswith("7\tline 7")


def test_read_file_truncates_overlong_lines(tmp_path: Path) -> None:
    from rebar.llm.pai_tools import _READ_MAX_LINE_CHARS

    (tmp_path / "min.js").write_text("x" * (_READ_MAX_LINE_CHARS + 4000) + "\n", encoding="utf-8")
    out = _fs_tools(tmp_path)["read_file"]("min.js")
    assert "truncated" in out
    assert len(out) < _READ_MAX_LINE_CHARS + 500  # the 4000-char tail was clipped


def test_read_tools_return_recoverable_error_for_missing_path(tmp_path: Path) -> None:
    """A missing/unreadable path (e.g. a file named in a diff but not on disk, or a
    directory) must return a recoverable ``Error:`` message — NOT raise an uncaught
    OSError that aborts the agent run. (A denied/escaping path is a separate block.)"""
    tools = _fs_tools(tmp_path)
    assert tools["read_file"]("does-not-exist.py").startswith("Error:")
    # read_file on a directory is also recoverable, not a crash.
    (tmp_path / "subdir").mkdir()
    assert tools["read_file"]("subdir").startswith("Error:")
    # list_directory on a missing path is recoverable too.
    assert tools["list_directory"]("no-such-dir").startswith("Error:")


def test_discovery_hides_noise_and_gitignored(rebar_repo: Path) -> None:
    """list_directory/search_files hide vendored/generated + .gitignore'd files, but
    read_file can still access an explicitly named one (large-project handling)."""
    (rebar_repo / ".gitignore").write_text("secret.txt\n", encoding="utf-8")
    (rebar_repo / "secret.txt").write_text("TOKEN=abc\n", encoding="utf-8")
    (rebar_repo / "visible.py").write_text("TOKEN_marker = 1\n", encoding="utf-8")
    (rebar_repo / "node_modules").mkdir()
    (rebar_repo / "node_modules" / "dep.js").write_text("TOKEN_marker\n", encoding="utf-8")

    tools = _fs_tools(rebar_repo)
    listing = tools["list_directory"](".")
    assert "visible.py" in listing
    assert "secret.txt" not in listing and "node_modules" not in listing
    # search skips the gitignored file and the vendored dir, finds the tracked one
    found = tools["search_files"]("TOKEN_marker")
    assert "visible.py" in found
    assert "secret.txt" not in found and "node_modules" not in found
    # but an explicitly named ignored file is still readable (not a security deny)
    assert "TOKEN=abc" in tools["read_file"]("secret.txt")


def test_discovery_rejects_symlink_escape(tmp_path: Path) -> None:
    """list_directory/search_files must not surface symlinks pointing outside the
    repo root — read_file already blocks them via _safe_path."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOPSECRET\n", encoding="utf-8")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "normal.txt").write_text("TOPSECRET marker\n", encoding="utf-8")
    (repo / "escape_dir").symlink_to(outside, target_is_directory=True)
    (repo / "escape_file").symlink_to(outside / "secret.txt")

    tools = _fs_tools(repo)
    listing = tools["list_directory"](".")
    assert "normal.txt" in listing
    assert "escape_dir" not in listing and "escape_file" not in listing
    found = tools["search_files"]("TOPSECRET")
    assert "normal.txt" in found and "secret.txt" not in found
    # An explicit read of an escaping symlink is BLOCKED, but as a recoverable
    # refusal message — not an uncaught ValueError that would abort the agent run.
    # (The secret content must never appear in the returned string.)
    refusal = tools["read_file"]("escape_file")
    assert refusal.startswith("Error:")
    assert "TOPSECRET" not in refusal
    # Same hard block, same recoverable shape, for an absolute path the model may pass.
    assert tools["list_directory"]("/").startswith("Error:")


def test_validate_rejects_bad_result() -> None:
    pytest.importorskip("jsonschema")
    from rebar.llm.findings import FindingsError, validate_result

    with pytest.raises(FindingsError):
        validate_result({"findings": [{"severity": "nope", "dimension": "d", "detail": "x"}]})


def test_pydantic_mirror_field_sets_match_schema() -> None:
    """Pin the Pydantic structured-output model to the JSON Schema $defs so the two
    can't drift (the schema is the source of truth)."""
    pytest.importorskip("pydantic")
    model = __import__(
        "rebar.llm.findings", fromlist=["findings_response_model"]
    ).findings_response_model
    Review = model()
    Finding = Review.model_fields["findings"].annotation.__args__[0]
    Citation = Finding.model_fields["citations"].annotation.__args__[0]

    common = schemas.load("common")["$defs"]
    assert set(Finding.model_fields) == set(common["finding"]["properties"]), (
        "Pydantic Finding fields drifted from common.schema.json finding $def"
    )
    assert set(Citation.model_fields) == set(common["citation"]["properties"]), (
        "Pydantic Citation fields drifted from common.schema.json citation $def"
    )


def test_normalize_clamps_soft_fields() -> None:
    from rebar.llm.findings import normalize_finding

    f = normalize_finding(
        {
            "severity": "high",
            "dimension": "d",
            "detail": "x",
            "confidence": 2.5,
            "citations": [{"kind": "file", "path": "a.py", "line_start": -3}],
        }
    )
    assert f["confidence"] == 1.0  # clamped into [0,1]
    assert "line_start" not in f["citations"][0]  # negative line dropped


def test_framework_errors_are_llmerror() -> None:
    """H1: the expected failure modes are catchable as one LLMError vocabulary."""
    import rebar.llm as llm
    from rebar.llm.findings import FindingsError
    from rebar.llm.prompts import ReviewerError

    assert issubclass(FindingsError, llm.LLMError)
    assert issubclass(ReviewerError, llm.LLMError)


# ── config + runner selection ─────────────────────────────────────────────────
def test_infer_provider() -> None:
    from rebar.llm.config import infer_provider

    assert infer_provider("claude-opus-4-8") == "anthropic"
    assert infer_provider("gpt-4o") == "openai"
    assert infer_provider("chatgpt-4o-latest") == "openai"
    assert infer_provider("gemini-2.5-pro") == "google_genai"
    assert infer_provider("openai:gpt-4o") == "openai"  # provider:model form
    assert infer_provider("local-model", explicit="openai") == "openai"
    assert infer_provider("mystery-model") is None


def test_model_string_selection_is_provider_agnostic() -> None:
    """The runtime picks the provider purely from the model string (no per-provider
    code): a bare name is provider-qualified, a qualified string is used verbatim."""
    pytest.importorskip("pydantic_ai")
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import _pai_model

    assert _pai_model(LLMConfig(model="claude-opus-4-8")) == "anthropic:claude-opus-4-8"
    assert _pai_model(LLMConfig(model="gpt-4o", model_provider="openai")) == "openai:gpt-4o"
    qualified = "google-gla:gemini-2.5-flash"
    assert _pai_model(LLMConfig(model=qualified)) == qualified


def test_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    from rebar.llm.config import LLMConfig

    # REBAR_LLM_RUNNER is removed (EV-4); the runner is DERIVED — pydantic_ai (d6d1).
    monkeypatch.delenv("REBAR_LLM_EXPERIMENTAL_HARNESS", raising=False)
    monkeypatch.setenv("REBAR_LLM_RUNNER", "fake")  # IGNORED — no longer a knob
    monkeypatch.setenv("REBAR_LLM_MODEL", "gpt-4o")
    monkeypatch.setenv("REBAR_LLM_MODEL_PROVIDER", "openai")
    monkeypatch.setenv("REBAR_LLM_BASE_URL", "http://localhost:1234/v1")
    monkeypatch.setenv("REBAR_LLM_MAX_ITERS", "7")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    cfg = LLMConfig.from_env(repo_root=".")
    # EV-4 CONTRACT (behavioral, not a hardcoded default): REBAR_LLM_RUNNER is IGNORED —
    # the runner is derived, so the env value "fake" must NOT take effect.
    assert cfg.runner != "fake"
    assert cfg.model == "gpt-4o" and cfg.max_iterations == 7
    assert cfg.model_provider == "openai" and cfg.base_url == "http://localhost:1234/v1"
    assert cfg.langfuse.enabled is True


def test_runner_derivation_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """EV-4 (behavioral): the runner is DERIVED, not settable from the environment. No env
    var changes it — neither the old REBAR_LLM_RUNNER knob nor the experimental harness one.
    Asserted as invariance (the derived value doesn't move), not a hardcoded runner name."""
    from rebar.llm.config import LLMConfig

    monkeypatch.delenv("REBAR_LLM_EXPERIMENTAL_HARNESS", raising=False)
    monkeypatch.delenv("REBAR_LLM_RUNNER", raising=False)
    derived = LLMConfig.from_env(repo_root=".").runner

    monkeypatch.setenv("REBAR_LLM_RUNNER", "fake")  # not a knob → ignored
    assert LLMConfig.from_env(repo_root=".").runner == derived
    monkeypatch.setenv("REBAR_LLM_EXPERIMENTAL_HARNESS", "deepagents")  # not a knob → ignored
    assert LLMConfig.from_env(repo_root=".").runner == derived
    # and the derived runner is a real, selectable runner (not None / typo)
    from rebar.llm.config import RUNNERS

    assert derived in RUNNERS


def test_runner_selection_and_stubs() -> None:
    from rebar.llm.config import RUNNERS, LLMConfig
    from rebar.llm.errors import LLMConfigError
    from rebar.llm.runner import FakeRunner, get_runner

    assert isinstance(get_runner(LLMConfig(runner="fake")), FakeRunner)
    # Behavioral (not a hardcoded default): get_runner dispatches to the runner the
    # config NAMES, so the resolved runner's name matches the config's runner field.
    for name in RUNNERS:
        assert get_runner(LLMConfig(runner=name)).name == name
    default_cfg = LLMConfig()
    assert get_runner(default_cfg).name == default_cfg.runner
    # An explicit override always wins over the config-named runner.
    fake = FakeRunner(findings=[{"severity": "low", "dimension": "d", "detail": "x"}])
    assert get_runner(LLMConfig(runner="pydantic_ai"), override=fake) is fake
    # An unknown (typo'd) library runner value fails loudly, naming the valid set.
    with pytest.raises(LLMConfigError, match="unknown runner"):
        get_runner(LLMConfig(runner="bogus"))


# ── code review + multi-reviewer aggregation ──────────────────────────────────
def test_aggregate_findings_clusters_and_ranks() -> None:
    from rebar.llm.aggregate import aggregate_findings

    r1 = {
        "reviewers": ["a"],
        "findings": [
            {
                "severity": "high",
                "dimension": "security",
                "detail": "sql injection",
                "citations": [{"kind": "file", "path": "db.py", "line_start": 10}],
            },
            {"severity": "low", "dimension": "style", "detail": "naming"},
        ],
    }
    r2 = {
        "reviewers": ["b"],
        "findings": [
            {
                "severity": "medium",
                "dimension": "security",
                "detail": "sqli risk",
                "citations": [{"kind": "file", "path": "db.py", "line_start": 12}],
            },
        ],
    }
    merged = aggregate_findings([r1, r2])
    assert len(merged) == 2  # the two db.py security findings cluster into one
    top = merged[0]
    assert top["dimension"] == "security" and top["severity"] == "high"  # representative
    assert top["agreement"] == 2 and top["reviewers"] == ["a", "b"]
    assert len(top["citations"]) == 2  # citations unioned
    assert merged[1]["dimension"] == "style"  # ranked below (lower severity/agreement)


def test_aggregate_clusters_across_line_bucket_boundary() -> None:
    """Two reviewers citing the same region at lines straddling a 10-line bucket
    boundary (9 vs 11) must still cluster — proximity, not fixed bucketing."""
    from rebar.llm.aggregate import aggregate_findings

    r1 = {
        "reviewers": ["a"],
        "findings": [
            {
                "severity": "high",
                "dimension": "security",
                "detail": "bug",
                "citations": [{"kind": "file", "path": "x.py", "line_start": 9}],
            },
        ],
    }
    r2 = {
        "reviewers": ["b"],
        "findings": [
            {
                "severity": "high",
                "dimension": "security",
                "detail": "same bug",
                "citations": [{"kind": "file", "path": "x.py", "line_start": 11}],
            },
        ],
    }
    merged = aggregate_findings([r1, r2])
    assert len(merged) == 1 and merged[0]["agreement"] == 2
    assert merged[0]["reviewers"] == ["a", "b"]
    # A far-away finding on the same file/dimension stays its own cluster.
    r3 = {
        "reviewers": ["c"],
        "findings": [
            {
                "severity": "high",
                "dimension": "security",
                "detail": "different",
                "citations": [{"kind": "file", "path": "x.py", "line_start": 99}],
            },
        ],
    }
    assert len(aggregate_findings([r1, r2, r3])) == 2


def test_changed_from_diff_covers_deletes_and_renames() -> None:
    from rebar.llm.code_review import _changed_from_diff

    diff = (
        "diff --git a/auth.py b/auth.py\n"
        "deleted file mode 100644\n--- a/auth.py\n+++ /dev/null\n"
        "diff --git a/old.py b/new.py\n"
        "similarity index 90%\nrename from old.py\nrename to new.py\n"
        "diff --git a/normal.py b/normal.py\n"
        "--- a/normal.py\n+++ b/normal.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    files = _changed_from_diff(diff)
    assert files == ["auth.py", "new.py", "normal.py"]  # delete + rename + edit, deduped
    assert "/dev/null" not in files


def test_select_code_reviewers_rules() -> None:
    from rebar.llm.code_review import select_code_reviewers

    assert select_code_reviewers(["README.md"]) == ["code-quality"]
    sel = select_code_reviewers(["src/rebar/auth.py", "tests/test_x.py"])
    assert sel[0] == "code-quality" and "security" in sel and "tests" in sel


def test_review_code_end_to_end(tmp_path: Path) -> None:
    import rebar.llm as llm
    from rebar.llm.config import LLMConfig

    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n+print('hi')\n"
    runner = llm.FakeRunner(
        findings=[
            {
                "severity": "high",
                "dimension": "code-quality",
                "detail": "bug",
                "citations": [{"kind": "source", "description": "from the diff"}],
            }
        ],
        summary="s",
    )
    result = llm.review_code(
        diff_text=diff,
        changed_files=["x.py"],
        reviewers=["code-quality", "security"],
        config=LLMConfig(repo_path=str(tmp_path)),
        runner=runner,
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["target"]["kind"] == "code" and result["target"]["files"] == ["x.py"]
    assert set(result["reviewers"]) == {"code-quality", "security"}
    # both reviewers raised the same finding → aggregated with agreement 2
    assert result["findings"][0]["agreement"] == 2
    assert sorted(result["findings"][0]["reviewers"]) == ["code-quality", "security"]


def test_review_code_derives_changed_files_from_diff(tmp_path: Path) -> None:
    import rebar.llm as llm
    from rebar.llm.config import LLMConfig

    diff = "--- a/a.py\n+++ b/a.py\n@@\n+x\n--- a/b.py\n+++ b/b.py\n@@\n+y\n"
    result = llm.review_code(
        diff_text=diff,
        reviewers=["code-quality"],
        config=LLMConfig(repo_path=str(tmp_path)),
        runner=llm.FakeRunner(findings=[]),
    )
    assert set(result["target"]["files"]) == {"a.py", "b.py"}  # parsed from +++ lines


# ── batch spec scan ───────────────────────────────────────────────────────────
def test_scan_epics_for_spec_batches(rebar_repo: Path) -> None:
    import rebar.llm as llm
    from rebar.llm.config import LLMConfig

    for i in range(3):
        rebar.create_ticket(
            "epic",
            f"Epic {i}",
            description=f"Body {i}.\n\n## Acceptance Criteria\n- [ ] x",
            repo_root=str(rebar_repo),
        )
    runner = llm.FakeRunner(
        findings=[
            {
                "severity": "high",
                "dimension": "spec-alignment",
                "detail": "gap",
                "citations": [{"kind": "source", "description": "epic"}],
            }
        ],
    )
    result = llm.scan_epics_for_spec(
        "The system must do X and Y.",
        batch_size=2,
        config=LLMConfig(repo_path=str(rebar_repo)),
        runner=runner,
        repo_root=str(rebar_repo),
    )
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["target"]["kind"] == "spec_scan"
    assert len(result["target"]["ticket_ids"]) == 3  # all open epics scanned
    # 3 epics @ batch_size 2 → 2 batches → FakeRunner's finding once per batch
    assert len(result["findings"]) == 2
    assert result["reviewers"] == ["spec-alignment"]


# ── review_ticket end-to-end (FakeRunner against a real store) ────────────────
def _seed(repo: Path) -> str:
    r = str(repo)
    epic = rebar.create_ticket("epic", "Login epic", repo_root=r)
    rebar.create_ticket(
        "task",
        "Add auth",
        description="Body.\n\n## Acceptance Criteria\n- [ ] login works",
        parent=epic,
        repo_root=r,
    )
    return epic


def test_review_ticket_end_to_end(rebar_repo: Path) -> None:
    import rebar.llm as llm

    epic = _seed(rebar_repo)
    (rebar_repo / "app.py").write_text("import os\nKEY='x'\n", encoding="utf-8")
    runner = llm.FakeRunner(
        findings=[
            {
                "severity": "high",
                "dimension": "security",
                "detail": "hardcoded secret",
                "citations": [{"kind": "file", "path": "app.py", "line_start": 2, "line_end": 2}],
            }
        ],
        summary="one issue",
    )
    result = llm.review_ticket(epic, "ticket-quality", repo_root=str(rebar_repo), runner=runner)
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["runner"] == "fake"
    assert result["reviewers"] == ["ticket-quality"]
    assert result["target"]["kind"] == "ticket"
    assert result["findings"][0]["citations"][0]["kind"] == "file"  # real file kept


def test_review_ticket_graph_includes_children(rebar_repo: Path) -> None:
    import rebar.llm as llm

    epic = _seed(rebar_repo)
    runner = llm.FakeRunner(findings=[])
    result = llm.review_ticket(epic, repo_root=str(rebar_repo), graph=True, runner=runner)
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["target"]["kind"] == "ticket_graph"
    assert len(result["target"]["ticket_ids"]) >= 2  # epic + its task


def test_review_ticket_unknown_reviewer_is_llmerror(rebar_repo: Path) -> None:
    import rebar.llm as llm

    epic = _seed(rebar_repo)
    with pytest.raises(llm.LLMError):
        llm.review_ticket(
            epic, "no-such-reviewer", repo_root=str(rebar_repo), runner=llm.FakeRunner()
        )


# ── CLI surface ───────────────────────────────────────────────────────────────
def test_cli_review_check(capsys: pytest.CaptureFixture) -> None:
    from rebar._cli import main

    rc = main(["review", "--check"])
    out = capsys.readouterr().out
    assert rc == 0
    data = json.loads(out)
    assert "pydantic_ai" in data and "anthropic_api_key" in data


def test_cli_review_with_fake_runner(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    epic = _seed(rebar_repo)
    # fake is off the public env surface (EV-4); inject it via the library seam the
    # CLI review path uses (operations.get_runner) — the only offline injection point.
    from rebar.llm import operations
    from rebar.llm.runner import FakeRunner

    monkeypatch.setattr(operations, "get_runner", lambda cfg, override=None: FakeRunner())
    from rebar._cli import main

    rc = main(["review", epic, "--output", "json"])
    out = capsys.readouterr().out
    assert rc == 0, out
    result = json.loads(out)
    schemas.validator(schemas.REVIEW_RESULT).validate(result)
    assert result["runner"] == "fake" and result["findings"] == []


def test_cli_review_bad_reviewer_is_graceful(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    epic = _seed(rebar_repo)
    from rebar.llm import operations
    from rebar.llm.runner import FakeRunner

    monkeypatch.setattr(operations, "get_runner", lambda cfg, override=None: FakeRunner())
    from rebar._cli import main

    rc = main(["review", epic, "no-such-reviewer"])
    err = capsys.readouterr().err
    assert rc == 1 and "Error:" in err  # clean error, not a traceback


# ── MCP surface ───────────────────────────────────────────────────────────────
def test_mcp_review_tool_registered_and_gated(
    rebar_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pytest.importorskip("mcp")
    import asyncio

    from adapters import _unwrap  # tests/interfaces on sys.path

    from rebar.mcp_server import build_server

    srv = build_server()
    tools = {t.name: t for t in asyncio.run(srv.list_tools())}
    # All three LLM tools are registered, plain-dict return → no advertised
    # outputSchema (NO_SCHEMA_EXEMPT contract).
    for name in ("review_ticket", "review_code", "scan_spec"):
        assert name in tools, name
        assert not tools[name].outputSchema, name

    epic = _seed(rebar_repo)
    # All three are disabled by default (no REBAR_MCP_ALLOW_LLM) → tool error, so a
    # default MCP client can never trigger a billable LLM call.
    monkeypatch.delenv("REBAR_MCP_ALLOW_LLM", raising=False)
    gated_calls = {
        "review_ticket": {"ticket_id": epic},
        "review_code": {},
        "scan_spec": {"spec_text": "the spec"},
    }
    for name, args in gated_calls.items():
        # FastMCP wraps the tool's ValueError in a transport error whose exact type
        # is version-dependent; we only need to assert the gated call errors.
        with pytest.raises(Exception):  # noqa: B017
            _unwrap(asyncio.run(srv.call_tool(name, args)))
