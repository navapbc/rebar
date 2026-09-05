"""Oracle for the EC2 ``user_data`` size gate [rebar:a68c-9633-248c-4b06].

The bug: ``infra/terraform/user_data.sh`` rendered to 16,668 bytes against EC2's 16,384-byte
``UserData`` cap, so ``terraform plan`` could not GENERATE for the entire configuration and
every apply was blocked. The gate under test is ``scripts/check_user_data_size.py``.

Each test names the property it asserts, and the two ``seeded`` tests exist to prove the gate
is load-bearing rather than incidentally green: they reintroduce the defect and require the
gate to go red again.
"""

from __future__ import annotations

import gzip
import hashlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "check_user_data_size.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

from check_user_data_size import (  # noqa: E402
    PLACEHOLDER_LEN,
    USER_DATA_LIMIT_BYTES,
    Payload,
    check_repo,
    render,
)

MAIN_TF = "infra/terraform/main.tf"
USER_DATA_SH = "infra/terraform/user_data.sh"


def _tree(tmp_path: Path, *, script_body: str, gzipped: bool) -> Path:
    """A minimal terraform tree with one ``aws_instance`` user_data call site."""
    tf_dir = tmp_path / "infra" / "terraform"
    tf_dir.mkdir(parents=True)
    (tf_dir / "user_data.sh").write_text(script_body, encoding="utf-8")
    call = 'templatefile("${path.module}/user_data.sh", {\n    vol = aws_ebs_volume.data.id\n  })'
    attribute = f"  user_data_base64 = base64gzip({call})" if gzipped else f"  user_data = {call}"
    (tf_dir / "main.tf").write_text(
        'resource "aws_instance" "gerrit" {\n' + attribute + "\n}\n", encoding="utf-8"
    )
    return tmp_path


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root)],
        capture_output=True,
        text=True,
        check=False,
    )


# ── the shipped configuration ────────────────────────────────────────────────────────────


def test_the_shipped_configuration_fits_under_the_ec2_limit() -> None:
    """The tree as committed must be plannable: this is the bug's own acceptance criterion."""
    results = check_repo(REPO_ROOT)
    payloads = [r for r in results if isinstance(r, Payload)]
    assert payloads, "the gate found no user_data call site, so it proved nothing"
    for payload in payloads:
        assert not payload.over, payload.render_report()


def test_the_shipped_user_data_is_gzipped_because_its_plain_render_does_not_fit() -> None:
    """The encoding is load-bearing, not stylistic.

    If the plain render fitted, ``base64gzip`` would be optional and a future contributor
    could drop it harmlessly. It does not fit, so this asserts the reason the wrapper exists.
    """
    payload = next(
        r for r in check_repo(REPO_ROOT) if isinstance(r, Payload) and r.template == USER_DATA_SH
    )
    assert payload.encoding == "gzipped"
    assert payload.rendered_bytes > USER_DATA_LIMIT_BYTES
    assert payload.transport_bytes <= USER_DATA_LIMIT_BYTES


def test_the_committed_main_tf_uses_user_data_base64_not_user_data() -> None:
    """``user_data`` takes the RAW string, which is the attribute that hit the provider cap."""
    text = (REPO_ROOT / MAIN_TF).read_text(encoding="utf-8")
    assert "user_data_base64 = base64gzip(templatefile(" in text
    assert "\n  user_data = templatefile(" not in text


# ── the property the AC names: rendered/encoded, never the raw file ──────────────────────


def test_the_gate_measures_the_rendered_payload_not_the_raw_file(tmp_path: Path) -> None:
    """A file UNDER the cap whose RENDER goes over must fail.

    This is the discriminator between the two candidate designs. A raw-file guard passes this
    tree; the payload terraform builds from it does not fit, so the gate must reject it.
    """
    interpolations = 20
    filler = "x" * (USER_DATA_LIMIT_BYTES - 200 - interpolations * len("${vol}"))
    body = filler + "${vol}" * interpolations
    assert len(body.encode()) < USER_DATA_LIMIT_BYTES, "the RAW file must be under the cap"

    root = _tree(tmp_path, script_body=body, gzipped=False)
    payload = next(r for r in check_repo(root) if isinstance(r, Payload))
    assert payload.rendered_bytes > USER_DATA_LIMIT_BYTES
    assert payload.over
    assert _run(root).returncode == 1


def test_the_gate_measures_the_encoded_payload_not_the_rendered_script(tmp_path: Path) -> None:
    """A render OVER the cap that gzips under it must pass — the shipped shape."""
    body = "# a very compressible comment line\n" * 900
    assert len(body.encode()) > USER_DATA_LIMIT_BYTES

    root = _tree(tmp_path, script_body=body, gzipped=True)
    payload = next(r for r in check_repo(root) if isinstance(r, Payload))
    assert payload.rendered_bytes > USER_DATA_LIMIT_BYTES
    assert payload.transport_bytes <= USER_DATA_LIMIT_BYTES
    assert not payload.over
    assert _run(root).returncode == 0


def test_the_substituted_estimate_is_an_upper_bound_on_a_real_value() -> None:
    """Placeholders must never make the gate optimistic.

    ``PLACEHOLDER_LEN`` has to exceed every value this repo interpolates, or the bound is not
    a bound. EBS volume ids are 21 characters and the scratch mount path 27.
    """
    assert PLACEHOLDER_LEN > len("vol-06780b8557d1416b7")
    assert PLACEHOLDER_LEN > len("/var/lib/rebar/gate-scratch")
    rendered = render("${a}", {"a": "x" * PLACEHOLDER_LEN})
    assert len(rendered) == PLACEHOLDER_LEN


#: The values terraform actually interpolates, read from the live plan that proved this fix
#: (``terraform plan`` 2026-09-05: aws_instance.gerrit updated in-place, 0 to destroy).
REAL_VALUES = {
    "data_volume_id": "vol-06fa2e77a9dd97527",
    "gate_scratch_volume_id": "vol-06780b8557d1416b7",
    "gate_scratch_mount": "/var/lib/rebar/gate-scratch",
}


def test_the_estimate_is_at_least_the_payload_built_from_the_real_values() -> None:
    """The gate's answer must never be smaller than the bytes AWS really receives.

    This is the property the placeholder substitution exists to guarantee, asserted
    end-to-end rather than by reasoning about entropy: render the shipped template with the
    values terraform genuinely interpolates, gzip it exactly as ``base64gzip`` does, and
    require the gate's reported figure to bound it. An optimistic gate is worse than none —
    it would pass the very configuration that cannot be planned.
    """
    payload = next(
        r for r in check_repo(REPO_ROOT) if isinstance(r, Payload) and r.template == USER_DATA_SH
    )
    truth = render((REPO_ROOT / USER_DATA_SH).read_text(encoding="utf-8"), REAL_VALUES).encode(
        "utf-8"
    )

    assert payload.rendered_bytes >= len(truth)
    assert payload.transport_bytes >= len(gzip.compress(truth, mtime=0))
    # And the true payload is genuinely over the cap before compression: that is the bug.
    assert len(truth) > USER_DATA_LIMIT_BYTES


def test_a_chained_placeholder_beats_a_repeated_one_for_bounding() -> None:
    """Seeded: the naive `digest * n` placeholder DEFLATE collapses, breaking the bound.

    Found by this oracle during development — the first implementation repeated one sha256
    hexdigest, which gzip reduced to a back-reference. Kept as a regression guard.
    """
    from check_user_data_size import placeholder

    chained = placeholder("gate_scratch_volume_id")
    repeated = (hashlib.sha256(b"gate_scratch_volume_id").hexdigest() * 8)[: len(chained)]
    assert len(chained) == len(repeated) == PLACEHOLDER_LEN
    assert len(gzip.compress(chained.encode(), mtime=0)) > len(
        gzip.compress(repeated.encode(), mtime=0)
    )


# ── templatefile() semantics the render must honour ──────────────────────────────────────


def test_render_treats_double_dollar_braces_as_literal_text() -> None:
    """``$${!PARAMS[@]}`` is bash, not an interpolation (bug dd30). Mis-rendering it would
    both mis-size the payload and mask an escaping defect."""
    assert render("$${!PARAMS[@]}", {}) == "${!PARAMS[@]}"
    assert render("a $${x} b ${x} c", {"x": "V"}) == "a ${x} b V c"


# ── anti-vacuity: an unmeasurable call site must fail, not pass ──────────────────────────


def test_an_unresolvable_template_path_fails_rather_than_being_skipped(tmp_path: Path) -> None:
    tf_dir = tmp_path / "infra" / "terraform"
    tf_dir.mkdir(parents=True)
    (tf_dir / "main.tf").write_text(
        'resource "aws_instance" "x" {\n'
        '  user_data = templatefile("${path.module}/absent.sh", {})\n}\n',
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "was NOT measured" in result.stdout


# ── defect seeding: prove the gate is load-bearing ───────────────────────────────────────


@pytest.mark.parametrize(
    ("seed", "why"),
    [
        ("drop-gzip", "removing base64gzip() restores the exact overflow terraform reported"),
        ("regrow-script", "appending comments back past the cap must be caught again"),
    ],
)
def test_seeded_defects_turn_the_gate_red_again(tmp_path: Path, seed: str, why: str) -> None:
    """Reintroduce the bug two ways; the gate must reject both."""
    tf_dir = tmp_path / "infra" / "terraform"
    tf_dir.mkdir(parents=True)
    script = (REPO_ROOT / USER_DATA_SH).read_text(encoding="utf-8")
    main_tf = (REPO_ROOT / MAIN_TF).read_text(encoding="utf-8")

    if seed == "drop-gzip":
        main_tf = main_tf.replace(
            "user_data_base64 = base64gzip(templatefile(", "user_data = templatefile("
        ).replace("  }))", "  })")
    else:
        # Keep the gzip wrapper and grow the script with incompressible content instead.
        script += "\n# " + "".join(f"{i:x}" for i in range(60000)) + "\n"

    (tf_dir / "user_data.sh").write_text(script, encoding="utf-8")
    (tf_dir / "main.tf").write_text(main_tf, encoding="utf-8")

    result = _run(tmp_path)
    assert result.returncode == 1, f"{why}: gate stayed green — it has no teeth"
    assert "OVER by" in result.stdout


# ── AC5: "no plan" must not read as "drift" in the daily sweep ───────────────────────────

DRIFT_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "terraform-drift.yml"


def test_the_drift_sweep_names_a_failed_plan_separately_from_drift() -> None:
    """Exit 1 and exit 2 are opposite conditions; the sweep must not merge them into one red.

    Exit 2 says "we looked and it differs". Exit 1 says "we could not look". This bug hid for
    a day because the workflow reported both as a bare failure and a red drift run reads as
    drift. The assertion is on the three-way branch, not on prose.
    """
    text = DRIFT_WORKFLOW.read_text(encoding="utf-8")
    plan_step = text.split("- name: terraform plan (fail on drift)", 1)[1]

    assert "-detailed-exitcode" in plan_step
    assert 'case "$rc" in' in plan_step, "the outcomes are not branched on at all"
    for arm in ("0)", "2)", "*)"):
        assert arm in plan_step, f"no arm for {arm}: an outcome is unnamed"
    assert "PLAN COULD NOT BE GENERATED" in plan_step
    assert "NOT drift" in plan_step
    assert "DETECTED" in plan_step


def test_the_drift_sweep_still_fails_on_both_non_zero_outcomes() -> None:
    """Naming the outcomes must not accidentally make either one non-fatal.

    A `case` that swallowed the exit code would turn a gating check into a reporting one —
    strictly worse than the conflation it set out to fix.
    """
    plan_step = DRIFT_WORKFLOW.read_text(encoding="utf-8").split(
        "- name: terraform plan (fail on drift)", 1
    )[1]
    assert 'exit "$rc"' in plan_step, "the step no longer propagates terraform's exit code"
    assert "set -o pipefail" in plan_step, "tee would mask terraform's exit code without it"
