"""CI parity: every gate that runs against `main` post-merge must also gate pre-merge.

The Gerrit `Verified` vote is cast by ``.github/workflows/gerrit-verify.yaml`` BEFORE a
change can land; ``.github/workflows/test.yml`` runs the same gates AFTER the fact on the
pushed `main`. The two step lists are hand-maintained, so they drift silently — and a gate
present only in ``test.yml`` can *only* fail post-merge, letting a red change land with a
green Verified vote (this is exactly how a stale ``docs/env-vars.md`` reached `main`).

These tests fail the build the moment the two workflows diverge, so drift is caught at
review time instead of on `main`. When you add a gate, add it to BOTH workflows (or, for a
deliberate asymmetry, record it in the allowlist below with a reason).
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_TEST_YML = _ROOT / ".github" / "workflows" / "test.yml"
_GERRIT_YML = _ROOT / ".github" / "workflows" / "gerrit-verify.yaml"

# Each gating check keyed by a STABLE command signature (not the step name — names differ
# in wording between the two files, e.g. the pip-audit step). Every signature here must
# appear in BOTH workflows: the pre-merge Verified gate and the post-merge branch CI.
_SHARED_GATE_SIGNATURES = {
    "module-size ratchet": "module-size-ceilings.txt",
    "prompt-index drift gate": "regenerate-index",
    "env-var registry drift gate": "scripts/gen_env_registry.py",
    "security-rules freshness gate": "security_pin",
    "criteria-routing parity gate": "validate-routing",
    "server.json env-contract drift gate": "scripts/check_server_manifest.py",
    "public-types drift gate": "gen_types",
    "lint (ruff)": "make lint",
    "mypy (typecheck)": "make typecheck",
    "config-check": "make config-check",
    "pre-commit all hooks": "pre-commit run --all-files",
    "pip-audit": "pip-audit",
    "default test suite": 'pytest -m "not integration and not external"',
    "integration tier": "pytest -m integration",
}

# Scripts referenced by a gate in test.yml that are DELIBERATELY not run in the Verified
# gate. Empty by design: a derive-and-diff drift gate (scripts/gen_*.py / check_*.py) that
# gates `main` must also gate pre-merge, or a broken artifact lands green. Add an entry
# only with a written reason.
_INTENTIONAL_SCRIPT_ASYMMETRIES: set[str] = set()


def _read(path: Path) -> str:
    assert path.exists(), f"workflow not found: {path}"
    return path.read_text()


def test_verified_gate_runs_every_shared_gate() -> None:
    """Each known gate signature is present in BOTH the Verified gate and branch CI."""
    test_yml = _read(_TEST_YML)
    gerrit_yml = _read(_GERRIT_YML)
    for label, sig in _SHARED_GATE_SIGNATURES.items():
        assert sig in test_yml, (
            f"gate {label!r} signature {sig!r} not found in test.yml — the signature is "
            "stale; update _SHARED_GATE_SIGNATURES to match the renamed/removed gate."
        )
        assert sig in gerrit_yml, (
            f"gate {label!r} ({sig!r}) runs in test.yml (post-merge branch CI) but is "
            "MISSING from gerrit-verify.yaml (the pre-merge Verified gate) — so it can "
            "only fail AFTER a change lands on main. Add it to gerrit-verify.yaml."
        )


def test_no_drift_script_gate_is_verified_only_in_branch_ci() -> None:
    """Auto-catch the drift class: any scripts/*.py a gate runs in test.yml must also be
    invoked in gerrit-verify.yaml (this is how the env-vars.md gate slipped through)."""
    import re

    test_yml = _read(_TEST_YML)
    gerrit_yml = _read(_GERRIT_YML)
    scripts_in_test = set(re.findall(r"scripts/[A-Za-z0-9_]+\.py", test_yml))
    scripts_in_gerrit = set(re.findall(r"scripts/[A-Za-z0-9_]+\.py", gerrit_yml))
    missing = scripts_in_test - scripts_in_gerrit - _INTENTIONAL_SCRIPT_ASYMMETRIES
    assert not missing, (
        "script-driven gate(s) run in test.yml (post-merge) but not in the Verified gate "
        f"(gerrit-verify.yaml): {sorted(missing)}. Add them to gerrit-verify.yaml so they "
        "gate pre-merge, or record a reasoned exception in _INTENTIONAL_SCRIPT_ASYMMETRIES."
    )
