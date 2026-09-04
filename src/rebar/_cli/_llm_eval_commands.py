"""LLM eval / config CLI command handlers — split from :mod:`rebar._cli._llm_commands`
to keep both units under the module-size cap. Covers the evaluation + onboarding
commands: ``rebar prompt eval`` (validate a git-canonical prompt's eval spec),
``rebar criteria eval`` (run a review criterion's calibration fixtures live), and
``rebar llm setup`` (the LLM-framework onboarding wizard). These handlers are
re-exported from :mod:`rebar._cli._llm_commands`, so ``main()`` in ``rebar._cli`` and
existing importers dispatch to them unchanged.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from rebar._cli._parser import guard_parse_errors
from rebar._cli._parsers.advanced import llm_eval as _llm_eval_parsers
from rebar._mcp_errors import js_safe_dumps


@guard_parse_errors
def _prompt(argv: list[str]) -> int:
    """``rebar prompt eval <id>`` → prompt evaluation (WS-G). Native intercept.

    Validates the git-tracked eval spec (offline; grader discipline + at_least(k) +
    coverage) and reports the DIRTY working-tree prompt's content hash (what would be
    evaluated). The live model run needs the ``agents`` extra + credentials (the eval
    CI); committing the prompt is required to apply a passing edit (git-canonical)."""

    parser = _llm_eval_parsers.build_prompt(prog="rebar prompt")

    args = parser.parse_args(argv)
    if args.cmd == "eval":
        return _prompt_eval(args)
    parser.print_help()
    return 1


def _prompt_eval(args: argparse.Namespace) -> int:

    from rebar import config
    from rebar.llm.errors import LLMError
    from rebar.llm.evals import eval as _eval
    from rebar.llm.prompting import prompts as _prompts

    try:
        repo_root = str(config.repo_root())
    except Exception:  # noqa: BLE001 — not in a repo (or unreadable) — fall open to repo_root=None
        repo_root = None
    try:
        spec = _eval.load_eval_spec(args.prompt_id, repo_root=repo_root)
    except LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    # The DIRTY working-tree prompt is what an eval would evaluate (WS-G1).
    dirty_hash = None
    try:
        dirty_hash = _prompts.prompt_content_hash(
            _prompts.get_prompt(args.prompt_id, repo_root=repo_root).text
        )
    except LLMError:
        pass  # a user-file prompt without a catalog reviewer — spec is still valid

    scorers = spec.get("scorers", [])
    report = {
        "prompt": spec.get("prompt"),
        "valid": True,
        "epochs": spec.get("epochs"),
        "gate": spec.get("gate"),
        "coverage_threshold": spec.get("coverage_threshold"),
        "gating_scorers": [s.get("name") for s in scorers if s.get("type") == "deterministic"],
        "report_scorers": [s.get("name") for s in scorers if s.get("type") == "llm-judge"],
        "gold_set_size": len(spec.get("gold_set", [])),
        "dirty_prompt_sha256": dirty_hash,
    }
    if args.output == "json":
        sys.stdout.write(js_safe_dumps(report) + "\n")
    else:
        sys.stdout.write(f"Eval spec for prompt {report['prompt']!r}: VALID\n")
        sys.stdout.write(
            f"  epochs={report['epochs']}  gate={report['gate']}  "
            f"coverage>={report['coverage_threshold']}\n"
        )
        sys.stdout.write(f"  gating (deterministic): {report['gating_scorers']}\n")
        sys.stdout.write(f"  reporting (llm-judge):  {report['report_scorers']}\n")
        sys.stdout.write(f"  gold-set samples: {report['gold_set_size']}\n")
        if dirty_hash:
            sys.stdout.write(f"  dirty prompt sha256: {dirty_hash[:16]}…\n")
        sys.stdout.write(
            "  → live run needs the `agents` extra + credentials (eval CI); commit the "
            "prompt to apply a passing edit.\n"
        )
    return 0


@guard_parse_errors
def _criteria(argv: list[str]) -> int:
    """``rebar criteria eval <id>`` → run a criterion's calibration fixtures LIVE (story
    55b8). Unlike ``rebar prompt eval`` (validate-only), this executes the criterion's
    must-fire/must-not-fire fixtures through its Pass-1 finder and prints the calibration
    view (recall / false-accept / Cohen's κ / agreement / N-run stability) so a maintainer
    can decide blocking/threshold informed. Needs the ``agents`` extra + credentials."""

    parser = _llm_eval_parsers.build_criteria(prog="rebar criteria")

    args = parser.parse_args(argv)
    if args.cmd == "eval":
        return _criteria_eval(args)
    parser.print_help()
    return 1


def _criteria_eval(args: argparse.Namespace) -> int:

    from rebar import config
    from rebar.llm.errors import LLMError
    from rebar.llm.evals import eval as _eval

    if args.criterion_id and args.changed_since:
        sys.stderr.write("Error: criterion id and --changed-since are mutually exclusive\n")
        return 2
    if args.runs < 1:
        sys.stderr.write("Error: --runs must be >= 1\n")
        return 2
    if not (args.criterion_id or "").strip():
        if args.changed_since:
            return _criteria_eval_changed_since(args)
        return _missing_criterion_error()

    repo_root = _repo_root_or_none(config)

    # Reject an unknown or cross-gate-ambiguous criterion up front (before touching fixtures).
    registry_status = _validate_criterion_id(args.criterion_id, repo_root)
    if registry_status != 0:
        return registry_status

    try:
        report = _eval.calibrate_criterion(args.criterion_id, repo_root=repo_root, runs=args.runs)
    except LLMError as exc:  # absent fixture / empty dataset / missing extra — user-actionable
        sys.stderr.write(f"Error: {exc}\n")
        return 1

    _write_criteria_report(report, output=args.output)
    return 0


def _missing_criterion_error() -> int:
    sys.stderr.write("Error: a criterion id is required (e.g. `rebar criteria eval F1`)\n")
    return 2


def _repo_root_or_none(config_module: object) -> str | None:
    try:
        return str(config_module.repo_root())  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 — not in a repo — fall open to repo_root=None
        return None


def _validate_criterion_id(criterion_id: str, repo_root: str | None) -> int:
    from rebar.llm.errors import LLMError

    try:
        from rebar.llm.code_review import registry as code_review_registry
        from rebar.llm.plan_review import registry as plan_review_registry

        active_plan_review = criterion_id in plan_review_registry.by_id(repo_root)
        active_code_review = criterion_id in code_review_registry.effective_criteria(repo_root)
        if active_plan_review and active_code_review:
            sys.stderr.write(
                f"Error: ambiguous criterion {criterion_id!r} is active in both "
                "plan_review and code_review\n"
            )
            return 1
        if not (active_plan_review or active_code_review):
            sys.stderr.write(
                f"Error: unknown criterion {criterion_id!r} (not in the effective registry; "
                "activate a project criterion in .rebar/criteria_routing.json first)\n"
            )
            return 1
    except LLMError as exc:
        sys.stderr.write(f"Error: {exc}\n")
        return 1
    return 0


def _criteria_eval_changed_since(args: argparse.Namespace) -> int:
    from rebar import config
    from rebar.llm.evals.changed_criteria import select_changed_criteria

    repo_root = _changed_since_repo_root(config)
    try:
        changed_paths = _changed_paths_since(args.changed_since, cwd=repo_root)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        sys.stderr.write(f"Error: git diff failed for --changed-since {args.changed_since!r}")
        if detail:
            sys.stderr.write(f": {detail}")
        sys.stderr.write("\n")
        return 1

    selection_root = Path(repo_root) if repo_root is not None else Path.cwd()
    selection = select_changed_criteria(changed_paths, selection_root)
    for criterion_id in selection.selected:
        sys.stdout.write(f"{criterion_id}\n")
    for unmapped in selection.unmapped:
        sys.stderr.write(
            f"warning: changed rubric path maps to no registry criterion: {unmapped}\n"
        )

    if _live_criteria_eval_available():
        _run_selected_criteria_live(selection.selected, repo_root=repo_root, args=args)
    return 0


def _changed_since_repo_root(config_module: object) -> str | None:
    try:
        proc = subprocess.run(  # raw-git-ok: read-only repository root query
            ["git", "rev-parse", "--show-toplevel"],
            cwd=Path.cwd(),
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return _repo_root_or_none(config_module)
    return proc.stdout.strip() or None


def _changed_paths_since(ref: str, *, cwd: str | None) -> list[str]:
    proc = subprocess.run(  # raw-git-ok: read-only changed-file query
        ["git", "diff", "--name-only", f"{ref}..HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _live_criteria_eval_available() -> bool:
    from rebar.llm.config import available_backends

    backends = available_backends()
    return bool(
        backends.get("pydantic_ai")
        and (backends.get("anthropic_api_key") or backends.get("openai_api_key"))
    )


def _run_selected_criteria_live(
    criterion_ids: tuple[str, ...], *, repo_root: str | None, args: argparse.Namespace
) -> None:
    from rebar.llm.errors import LLMError
    from rebar.llm.evals import eval as _eval

    for criterion_id in criterion_ids:
        try:
            report = _eval.calibrate_criterion(criterion_id, repo_root=repo_root, runs=args.runs)
        except LLMError as exc:
            sys.stderr.write(f"Error: {criterion_id}: {exc}\n")
            continue
        _write_criteria_report(report, output=args.output)


def _write_criteria_report(report: dict, *, output: str) -> None:
    if output == "json":
        sys.stdout.write(js_safe_dumps(report) + "\n")
        return

    def _pct(v: float | None) -> str:
        return "—" if v is None else f"{v * 100:.0f}%"

    sys.stdout.write(f"Calibration for criterion {report['criterion']!r} ({report['prompt']}):\n")
    sys.stdout.write(
        f"  fixtures: {report['n_fire']} must-fire, {report['n_nofire']} must-not-fire, "
        f"{report['n_discrimination']} discrimination  (runs={report['runs']})\n"
    )
    sys.stdout.write(f"  recall (must-fire fired):        {_pct(report['recall'])}\n")
    sys.stdout.write(f"  false-accept (must-not-fire fired): {_pct(report['false_accept'])}\n")
    sys.stdout.write(f"  agreement (observed==expected):  {_pct(report['agreement'])}\n")
    sys.stdout.write(f"  Cohen's κ:                       {report['kappa']:.2f}\n")
    sys.stdout.write(
        f"  stability:                       min {_pct(report['stability_min'])}, "
        f"mean {_pct(report['stability_mean'])}\n"
    )


@guard_parse_errors
def _llm(argv: list[str]) -> int:
    """``rebar llm setup`` → the LLM-framework onboarding wizard (WS-J2).

    Top-level help is served from the committed parser artifact. Child help is rendered by
    this parser. The command detects installed extras and API keys, validates the engine with
    an offline FakeRunner dry run, and prints the recommended ``[tool.rebar.llm]`` config."""

    parser = _llm_eval_parsers.build_llm(prog="rebar llm")

    args = parser.parse_args(argv)
    if args.cmd == "setup":
        return _llm_setup(args)
    parser.print_help()
    return 1


def _llm_setup(args: argparse.Namespace) -> int:

    import rebar
    from rebar import _optional
    from rebar import config as _rebar_config
    from rebar.llm import config as _cfg

    backends = _cfg.available_backends()
    extras = {e: _optional.extra_installed(e) for e in ("agents", "tracing")}

    # Validate the configuration with an offline FakeRunner dry-run (no tokens):
    # if a trivial agent workflow runs, the engine + config are wired correctly.
    dry_ok, dry_err = True, None
    try:
        res = rebar.run_workflow(
            {
                "schema_version": "1",
                "name": "setup_check",
                "steps": [{"id": "check", "prompt": "code_quality", "mode": "text"}],
            },
            dry_run=True,
        )
        dry_ok = res["status"] == "succeeded"
        dry_err = res.get("error")
    except Exception as exc:  # noqa: BLE001 - report any failure as a clean message
        dry_ok, dry_err = False, str(exc)

    # Optionally configure the OTLP tracing sink (write-only): an explicit
    # --otlp-endpoint, else the standard OTEL env var if set.
    otlp = _rebar_config.resolve_otlp_endpoint(args.otlp_endpoint)
    snippet = f'[tool.rebar.llm]\nmodel = "{_cfg.DEFAULT_MODEL}"\n'
    if otlp:
        snippet += (
            "\n# Write-only OTLP trace sink (OTel is never read back into a rebar "
            f'decision).\n[tool.rebar.llm.tracing]\notlp_endpoint = "{otlp}"\n'
        )
    report = {
        "extras": extras,
        "anthropic_api_key": backends["anthropic_api_key"],
        "openai_api_key": backends["openai_api_key"],
        "tracing_configured": backends.get("langfuse_configured", False),
        "otlp_endpoint": otlp,
        "dry_run_ok": dry_ok,
        "dry_run_error": dry_err,
        "recommended_config": snippet,
    }

    if args.output == "json":
        sys.stdout.write(js_safe_dumps(report) + "\n")
    else:
        sys.stdout.write("rebar LLM setup\n")
        sys.stdout.write(f"  extras:   agents={extras['agents']}  tracing={extras['tracing']}\n")
        sys.stdout.write(
            f"  API keys: anthropic={backends['anthropic_api_key']}  "
            f"openai={backends['openai_api_key']}\n"
        )
        sys.stdout.write(
            f"  FakeRunner dry-run: {'OK' if dry_ok else 'FAILED: ' + (dry_err or '')}\n"
        )
        otlp_line = otlp or "not configured (--otlp-endpoint URL or $OTEL_EXPORTER_OTLP_ENDPOINT)"
        sys.stdout.write(f"  OTLP tracing sink: {otlp_line}\n")
        if not extras["agents"]:
            sys.stdout.write(
                "  → for real agent steps install:  pip install 'nava-rebar[agents]'\n"
            )
        if not (backends["anthropic_api_key"] or backends["openai_api_key"]):
            sys.stdout.write("  → set a provider API key (e.g. ANTHROPIC_API_KEY)\n")
        sys.stdout.write(f"\nRecommended config:\n\n{snippet}\n")

    if args.write:
        try:
            with open(args.write, "w", encoding="utf-8") as fh:
                fh.write(snippet)
            sys.stdout.write(f"Wrote {args.write}\n")
        except OSError as exc:
            sys.stderr.write(f"Error: cannot write {args.write}: {exc}\n")
            return 1
    return 0 if dry_ok else 1
