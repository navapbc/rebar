"""Terraform structural grounding for the code-review gate (REB-640 / afe3).

The optional bridge between the code-review gate and the generic Terraform tool session
(``plan_review.terraform_seam``). It mints that session for the code-review IaC finder
(``code-review-iac``) and the Pass-2 verifier (``code-review-verify``), and folds the reads
those tools perform into the verdict usage so grounded findings are auditable.

Everything here is gated on the CHANGED set having Terraform scope (the ticket is grounding in
*changed* Terraform modules): a python-only diff never mints TF tools even when a merged IaC
finding happens to cite a ``.tf``. Absent the terraform tooling the seam yields no tools and
review behavior is unchanged, so the whole feature is optional.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

_TF_CITED_SUFFIXES = (".tf", ".tf.json")
_TF_SCOPE_SUFFIXES = (*_TF_CITED_SUFFIXES, ".tfvars")


def _terraform_paths_from_finding(finding: dict[str, Any]) -> list[str]:
    from rebar.llm.code_review.workflow_ops import _file_from_location

    paths: list[str] = []
    location = finding.get("location")
    if isinstance(location, dict):
        paths.append(str(location.get("file") or ""))
    elif isinstance(location, str):
        paths.append(_file_from_location(location))
    for key in ("files", "cited_files", "paths", "evidence"):
        value = finding.get(key)
        if isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    paths.append(_file_from_location(item))
                elif isinstance(item, dict):
                    paths.append(str(item.get("file") or item.get("path") or ""))
    return [p for p in paths if any(p.endswith(suffix) for suffix in _TF_CITED_SUFFIXES)]


def iac_terraform_findings(findings: object) -> list[dict]:
    out: list[dict] = []
    if not isinstance(findings, (list, tuple)):
        return out
    for finding in findings:
        if not isinstance(finding, dict) or finding.get("_shed") or finding.get("_too_big"):
            continue
        criteria = finding.get("criteria") or []
        is_iac = finding.get("reviewer_id") == "code-review-iac" or (
            isinstance(criteria, (list, tuple, set, frozenset)) and "iac" in criteria
        )
        if is_iac and _terraform_paths_from_finding(finding):
            out.append(finding)
    return out


def any_iac_terraform_evidence(findings: object) -> bool:
    return bool(iac_terraform_findings(findings))


def changed_terraform_scope(changed_files: object) -> bool:
    if isinstance(changed_files, str):
        return changed_files.endswith(_TF_SCOPE_SUFFIXES)
    if not isinstance(changed_files, (list, tuple, set, frozenset)):
        return False
    return any(
        isinstance(path, str) and path.endswith(_TF_SCOPE_SUFFIXES) for path in changed_files
    )


def _selected_terraform_paths_from_iac_findings(findings: object) -> list[str]:
    return sorted(
        {
            path
            for finding in iac_terraform_findings(findings)
            for path in _terraform_paths_from_finding(finding)
        }
    )


def build_code_review_tf_provider(
    *, repo_root: str, changed_files: object, usage_sink: dict[str, Any]
) -> Callable[[Any], tuple[list, Callable[[], None]] | None]:
    """Mint the Terraform tool session for the code-review IaC finder and Pass-2 verifier.

    Returns ``None`` (no tools) unless the CHANGED set has Terraform scope and either the
    ``code-review-iac`` prompt is running (grounded against the changed ``.tf`` set) or the
    ``code-review-verify`` prompt is running against a merged IaC finding that cites a ``.tf``.
    """
    from rebar.llm.plan_review import terraform_seam

    def _selected_changed() -> list[str]:
        if not isinstance(changed_files, (list, tuple, set, frozenset)):
            return []
        return [
            path
            for path in changed_files
            if isinstance(path, str) and path.endswith(_TF_SCOPE_SUFFIXES)
        ]

    def provider(ctx: Any) -> tuple[list, Callable[[], None]] | None:
        step = getattr(ctx, "step", None)
        prompt = (step if isinstance(step, dict) else {}).get("prompt")
        selected: list[str] = []
        if prompt == "code-review-iac" and changed_terraform_scope(changed_files):
            selected = _selected_changed()
        elif prompt == "code-review-verify" and changed_terraform_scope(changed_files):
            inputs = getattr(ctx, "inputs", None)
            findings = (inputs if isinstance(inputs, dict) else {}).get("findings")
            selected = _selected_terraform_paths_from_iac_findings(findings)
        if not selected:
            return None
        return terraform_seam.build_tool_provider(
            repo_root=repo_root, selected=selected, usage_sink=usage_sink, force=True
        )(ctx)

    return provider


def build_provider_and_sink(
    repo_root: str | None, changed_files: object
) -> tuple[Callable[[Any], tuple[list, Callable[[], None]] | None], dict[str, Any]]:
    """Construct the code-review TF provider and the usage sink its reads accumulate into."""
    sink: dict[str, Any] = {}
    provider = build_code_review_tf_provider(
        repo_root=cast(str, repo_root), changed_files=changed_files, usage_sink=sink
    )
    return provider, sink


def fold_tf_grounding_usage(verdict: dict[str, Any], tf_usage_sink: dict[str, Any]) -> None:
    """Fold the Terraform tool provider's distinct_fetches into the verdict usage.

    Best-effort: the grounding reads are a usage/audit signal, never a gate outcome, so a
    malformed usage shape is swallowed rather than failing the review.
    """
    try:
        usage = verdict.setdefault("_usage", {})
        if not isinstance(usage, dict):
            return
        merged = list(usage.get("distinct_fetches", []) or [])
        seen = {(f.get("tool"), f.get("target")) for f in merged if isinstance(f, dict)}
        for fetch in tf_usage_sink.get("distinct_fetches", []) or []:
            key = (fetch.get("tool"), fetch.get("target")) if isinstance(fetch, dict) else None
            if key and key not in seen:
                merged.append(fetch)
                seen.add(key)
        usage["distinct_fetches"] = merged
    except Exception:  # noqa: BLE001 — usage is best-effort; never fails the gate
        pass
