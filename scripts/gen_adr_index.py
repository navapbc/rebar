#!/usr/bin/env python3
"""Generate the ADR index (``docs/adr/README.md``) and the per-number marker files
(``docs/adr/.numbers/NNNN``) from the ADR corpus (story 0743).

Both artifacts are DERIVED from ``docs/adr/*.md`` and committed:

  * ``docs/adr/.numbers/NNNN`` — one marker per ADR, content = the ADR filename. Two
    ADRs claiming one number produce an add/add git conflict (the race-free uniqueness
    mechanism); ``scripts/check_adr_numbers.py`` is the CI backstop that asserts the
    bijection on the merged tree.
  * ``docs/adr/README.md`` — an index grouped by workstream, full slugs, sorted by
    number within each group.

Usage:
  python scripts/gen_adr_index.py            # (re)write README.md + markers
  python scripts/gen_adr_index.py --check     # exit 1 if the committed artifacts drift
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADR_DIR = REPO_ROOT / "docs" / "adr"
MARKERS_DIRNAME = ".numbers"
INDEX_NAME = "README.md"

# Ordered (label, keyword-substrings). First match on the slug wins; the fallback
# bucket catches anything unmatched. Order matters — more specific groups first.
WORKSTREAMS: list[tuple[str, tuple[str, ...]]] = [
    (
        "Code review",
        (
            "code-review",
            "review-bot",
            "voter",
            "novelty",
            "overlap",
            "public-exposure",
            "security-detectors",
            "retire-single-pass",
            "code-drift",
            "code-grounding",
        ),
    ),
    (
        "Plan review & criteria",
        (
            "plan-review",
            "criteria",
            "det-invariants",
            "det",
            "container-leaf",
            "grandfather",
            "convergent-plan",
            "re-review",
            "validation-consistency",
            "trust-boundary",
            "completion-aware",
            "graded-subanswer",
            "hedge-detector",
            "routed-enumeration",
        ),
    ),
    (
        "Reconciler & Jira sync",
        (
            "reconciler",
            "adapter",
            "jira",
            "acli",
            "transport-retry",
            "bound",
            "binding",
            "bidirectional",
            "three-way-merge",
            "ref-lock",
            "snapshot",
            "schema-derived",
            "gen-types",
            "impact-model",
            "vendor-adapter",
            "live-validation",
        ),
    ),
    (
        "LLM framework",
        (
            "llm",
            "prompt",
            "structured",
            "diagnostic",
            "pydantic-ai",
            "runner",
            "model-classes",
            "provider-seam",
            "stage-seam",
            "web-search",
            "burr",
            "gate-context",
            "enumeration-overlays",
        ),
    ),
    (
        "Git / Gerrit workflow",
        (
            "gerrit",
            "github",
            "mirror",
            "replication",
            "fast-forward",
            "submit",
            "two-vote",
            "ci-gate",
            "webhook",
            "ssh-vote",
            "feature-branch",
            "auto-lander",
            "autolander",
            "rebase-if-necessary",
            "review-label",
            "carry-trivial-rebase",
        ),
    ),
    (
        "Infra & deploy",
        (
            "iac",
            "secrets",
            "autodeploy",
            "on-box",
            "liveness",
            "watchdog",
            "foreground-git",
            "rehome",
            "shared-infra",
            "governance-artifacts",
        ),
    ),
    (
        "Store & event sourcing",
        (
            "snapshot-contract",
            "snapshot-cache",
            "reopen-invalidation",
            "batched-commit",
            "bridge-state",
            "event",
        ),
    ),
    (
        "Attestation & security",
        (
            "attest",
            "opcert",
            "operator-attested",
            "asymmetric",
            "authenticated-identity",
            "security-posture",
            "measurement-provenance",
            "completion-evidence",
            "shutdown",
            "in-flight",
            "coverage-gap",
        ),
    ),
    (
        "Config & CLI",
        (
            "config-write",
            "onboard",
            "editing-prompt-contracts",
            "aliases",
            "module-size",
            "unified-criteria-registry",
            "mcp-optional-auth",
            "calibration",
            "exit-11",
            "workflow-as-gate",
            "execution-mode",
        ),
    ),
]
FALLBACK = "Other decisions"


def _title(md_path: Path) -> str:
    """The ADR title = first ``# `` heading, minus the ``ADR NNNN — / :`` prefix."""
    for line in md_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            h = line[2:].strip()
            h = re.sub(r"^ADR\s+\d{4}\s*[—:-]\s*", "", h)
            h = re.sub(r"^\d{4}\s*[—:-]\s*", "", h)
            return h
    return md_path.stem


def _adr_files(adr_dir: Path) -> list[Path]:
    return sorted(p for p in adr_dir.glob("*.md") if p.name[:4].isdigit())


def _classify(slug: str) -> str:
    for label, keys in WORKSTREAMS:
        if any(k in slug for k in keys):
            return label
    return FALLBACK


def render_index(adr_dir: Path) -> str:
    groups: dict[str, list[Path]] = {}
    for p in _adr_files(adr_dir):
        slug = p.stem[5:]  # strip 'NNNN-'
        groups.setdefault(_classify(slug), []).append(p)

    order = [label for label, _ in WORKSTREAMS] + [FALLBACK]
    out: list[str] = [
        "# Architecture Decision Records",
        "",
        "<!-- GENERATED by scripts/gen_adr_index.py — do not edit by hand. -->",
        "",
        "ADR numbers are unique and collision-proof: each ADR owns a marker file in",
        "[`.numbers/`](.numbers/) and CI (`scripts/check_adr_numbers.py`) asserts the",
        "bijection. History of the 2026-08 renumbering is in [RENUMBERING.md](RENUMBERING.md).",
        "",
    ]
    for label in order:
        items = groups.get(label)
        if not items:
            continue
        out.append(f"## {label}")
        out.append("")
        for p in sorted(items, key=lambda x: x.name):
            num = p.name[:4]
            out.append(f"- [{num} — {_title(p)}]({p.name})")
        out.append("")
    return "\n".join(out).rstrip() + "\n"


def write_markers(adr_dir: Path) -> dict[str, str]:
    """Return the intended marker map {NNNN: filename}; caller writes/compares."""
    return {p.name[:4]: p.name for p in _adr_files(adr_dir)}


def generate(adr_dir: Path) -> None:
    markers_dir = adr_dir / MARKERS_DIRNAME
    markers_dir.mkdir(exist_ok=True)
    intended = write_markers(adr_dir)
    for existing in markers_dir.iterdir():
        if existing.is_file() and existing.name not in intended:
            existing.unlink()
    for num, fname in intended.items():
        (markers_dir / num).write_text(fname + "\n", encoding="utf-8")
    (adr_dir / INDEX_NAME).write_text(render_index(adr_dir), encoding="utf-8")


def _drift(adr_dir: Path) -> list[str]:
    problems: list[str] = []
    index_path = adr_dir / INDEX_NAME
    if not index_path.exists() or index_path.read_text(encoding="utf-8") != render_index(adr_dir):
        problems.append(f"{index_path} is stale — run scripts/gen_adr_index.py")
    markers_dir = adr_dir / MARKERS_DIRNAME
    intended = write_markers(adr_dir)
    on_disk = (
        {
            p.name: p.read_text(encoding="utf-8").strip()
            for p in markers_dir.iterdir()
            if p.is_file()
        }
        if markers_dir.is_dir()
        else {}
    )
    if {k: v for k, v in on_disk.items()} != intended:
        problems.append(f"{markers_dir} markers are stale — run scripts/gen_adr_index.py")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--adr-dir", type=Path, default=DEFAULT_ADR_DIR)
    ap.add_argument("--check", action="store_true", help="exit 1 if committed artifacts drift")
    ns = ap.parse_args(argv)
    if ns.check:
        problems = _drift(ns.adr_dir)
        for p in problems:
            print(p, file=sys.stderr)
        return 1 if problems else 0
    generate(ns.adr_dir)
    print(f"generated {ns.adr_dir / INDEX_NAME} and {ns.adr_dir / MARKERS_DIRNAME}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
