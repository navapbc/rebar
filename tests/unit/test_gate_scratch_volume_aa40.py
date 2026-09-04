"""Oracle for the dedicated gate-scratch volume (story aa40-cbda-ee38-481c).

ADR 0112 decision 3 moves the gate snapshot store and the review-bot's per-review clones off
the root filesystem onto their own EBS volume. Most of that is configuration —
``REBAR_GATE_TMPDIR`` already parameterises the store's base directory and
``tempfile.TemporaryDirectory`` already honours ``TMPDIR`` — so the tests here fall into two
groups.

**The refusal** is the one part that needed code, and it is the part the ADR is emphatic
about: a scratch volume that is not mounted must be a REFUSAL, "not an empty cache to
repopulate onto the root filesystem — otherwise the volume's failure mode is silently
reverting to the state this ADR exists to prevent". A bare mount point is an ordinary
directory, so without this guard ``store_root()``'s ``mkdir(parents=True)`` recreates the
store on root and everything keeps working, quietly, on the disk the epic exists to protect.

Two marker files make the two states distinguishable with NO new mechanism — no env var, no
config key, so ``check_mechanism_delta.py --check`` stays at ``new=0``:

* ``<base>/../.gate-scratch-required`` — the DECLARATION. Written on the ROOT filesystem
  beside the mount point, so it SURVIVES an unmount.
* ``<base>/.gate-scratch-mounted`` — the PROOF. Written ON the volume, so it DISAPPEARS with
  it.

Only ``declaration present AND proof absent`` refuses. Every other combination — including the
declaration-absent/proof-present quadrant that arises if the root-side write failed or an
operator removed it during recovery — resolves to today's behaviour, because a host that never
declared a dedicated volume must not start failing gates.

**The wiring** group asserts the committed Terraform / compose / observability text, following
``tests/unit/test_alarm_actions_terraform.py``: offline text contracts on the IaC, since a live
``terraform plan`` needs credentials and an apply is an operator action.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from rebar.llm import gate_admission as ga
from rebar.llm.errors import GateScratchUnavailableError

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
TF_DIR = REPO_ROOT / "infra" / "terraform"
COMPOSE = REPO_ROOT / "infra" / "compose" / "docker-compose.yml"

#: The host path the whole slice agrees on. Terraform's default, the compose bind, the
#: observability probe and the runbooks must all name it; a mismatch between any two of them
#: is a silent fallback to root, which is the failure this story removes.
SCRATCH_MOUNT = "/var/lib/rebar/gate-scratch"


@pytest.fixture
def scratch_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An isolated 'host': a mount-point directory whose PARENT stands in for the root FS.

    ``tmp_path/var`` is the durable root-side directory (it always exists) and
    ``tmp_path/var/gate-scratch`` is the mount point. Mounting is simulated by writing the
    proof marker inside it; unmounting, by removing it — which is exactly what an unmount
    does to a file that lived on the volume.
    """
    parent = tmp_path / "var"
    base = parent / "gate-scratch"
    base.mkdir(parents=True)
    monkeypatch.setenv("REBAR_GATE_TMPDIR", str(base))
    from rebar import _config_sources

    monkeypatch.setattr(_config_sources, "user_config_path", lambda: tmp_path / "absent.toml")
    (base / "rebar.toml").write_text("[snapshot]\nmax_concurrent_gates = 2\n")

    def declare() -> None:
        (parent / ga._SCRATCH_REQUIRED_MARKER).write_text("")

    def mount() -> None:
        (base / ga._SCRATCH_MOUNTED_MARKER).write_text("")

    def unmount() -> None:
        (base / ga._SCRATCH_MOUNTED_MARKER).unlink()

    return SimpleNamespace(base=base, declare=declare, mount=mount, unmount=unmount)


# ── the refusal ──────────────────────────────────────────────────────────────────────


def test_declared_but_unmounted_scratch_refuses_the_gate(scratch_host) -> None:
    """The ADR's consequence, stated as a test: no verdict, no fallback to root."""
    host = scratch_host
    host.declare()
    host.mount()
    host.unmount()

    with pytest.raises(GateScratchUnavailableError) as excinfo:
        with ga.gate_admission("plan_review", "t-1", host.base):
            pytest.fail("the gate ran on an unmounted scratch volume")

    assert "unreachable" in str(excinfo.value)


def test_the_refusal_creates_no_store_on_the_underlying_directory(scratch_host) -> None:
    """The REASON the check runs first: ``store_root()`` would mkdir the store on root.

    Without the pre-check the refusal still happens (or does not), but the snapshot store
    directory is materialised on the root filesystem on the way there — which is the silent
    revert, just with an error printed after it.
    """
    host = scratch_host
    host.declare()

    with pytest.raises(GateScratchUnavailableError):
        with ga.gate_admission("verify_completion", "t-2", host.base):
            pass

    from rebar._snapshot.repo_snapshot import _STORE_DIRNAME

    assert not (host.base / _STORE_DIRNAME).exists(), "the store was created on the unmounted path"


def test_a_mounted_scratch_volume_admits_the_gate(scratch_host) -> None:
    """The happy path: declaration AND proof present, so the gate runs normally."""
    host = scratch_host
    host.declare()
    host.mount()
    ran = []
    with ga.gate_admission("plan_review", "t-3", host.base):
        ran.append(True)
    assert ran == [True]


def test_no_declaration_means_no_assertion(scratch_host) -> None:
    """A host that never declared a dedicated volume keeps today's behaviour.

    This is what keeps the guard off every laptop, CI runner and existing operator box: the
    check is opt-in by provisioning, not by rebar version.
    """
    host = scratch_host
    ran = []
    with ga.gate_admission("plan_review", "t-4", host.base):
        ran.append(True)
    assert ran == [True]


def test_proof_without_declaration_also_admits(scratch_host) -> None:
    """The fourth quadrant, stated rather than left to fall out of the implementation.

    Proof-present/declaration-absent arises when the root-side write failed or an operator
    removed it during a recovery. It resolves to today's behaviour: the declaration is the
    only thing that turns the assertion on, so a missing declaration can never REFUSE.
    """
    host = scratch_host
    host.mount()
    ran = []
    with ga.gate_admission("verify_completion", "t-5", host.base):
        ran.append(True)
    assert ran == [True]


# ── Terraform: the volume itself ─────────────────────────────────────────────────────


def _tf_text() -> str:
    return (TF_DIR / "main.tf").read_text()


def test_terraform_declares_a_dedicated_gate_scratch_volume() -> None:
    src = _tf_text()
    assert 'resource "aws_ebs_volume" "gate_scratch"' in src
    assert 'resource "aws_volume_attachment" "gate_scratch"' in src
    assert "var.gate_scratch_volume_size_gb" in src
    assert 'output "gate_scratch_volume_id"' in src


def test_the_scratch_volume_is_rebuildable_not_source_of_truth() -> None:
    """``prevent_destroy`` guards the DATA volume precisely because it cannot be rebuilt.

    Scratch can: every byte on it is a re-materialisable snapshot or a re-clonable working
    copy. Marking it protected would make an ordinary teardown need a manual override for a
    volume nobody needs to keep, so the absence here is a decision, not an omission.
    """
    src = _tf_text()
    start = src.index('resource "aws_ebs_volume" "gate_scratch"')
    end = (
        src.index('resource "aws_instance"', start)
        if 'resource "aws_instance"' in src[start:]
        else len(src)
    )
    block = src[start:end]
    assert "prevent_destroy" not in block
    assert "rebuildable" in block.lower()


def test_user_data_receives_and_mounts_the_scratch_volume() -> None:
    """The template variable must be DECLARED by main.tf and USED by the script.

    ``templatefile()`` fails the whole configuration when the two disagree, and it evaluates
    every file in the module — so this pairing is worth pinning offline rather than finding
    at plan time.
    """
    main = _tf_text()
    script = (TF_DIR / "user_data.sh").read_text()
    # Whitespace-insensitive: `terraform fmt` aligns the `=` inside the map, so pinning
    # the exact run of spaces would make a later key rename fail this test for formatting.
    assert re.search(r"gate_scratch_volume_id\s*=\s*aws_ebs_volume\.gate_scratch\.id", main)
    assert re.search(r"gate_scratch_mount\s*=\s*var\.gate_scratch_mount", main)
    assert "${gate_scratch_volume_id}" in script
    assert "${gate_scratch_mount}" in script


def test_user_data_fails_loud_when_the_scratch_mount_did_not_take() -> None:
    """A silently-unmounted scratch volume is the failure mode this whole story removes."""
    script = (TF_DIR / "user_data.sh").read_text()
    assert "mountpoint -q" in script
    assert ga._SCRATCH_MOUNTED_MARKER in script
    assert ga._SCRATCH_REQUIRED_MARKER in script


def test_variables_declare_the_scratch_size_and_mount() -> None:
    src = (TF_DIR / "variables.tf").read_text()
    assert 'variable "gate_scratch_volume_size_gb"' in src
    assert 'variable "gate_scratch_mount"' in src
    assert SCRATCH_MOUNT in src


# ── compose: where the container's gate scratch actually lands ───────────────────────


def _review_bot_service() -> dict:
    doc = yaml.safe_load(COMPOSE.read_text())
    return doc["services"]["review-bot"]


def test_review_bot_snapshot_store_and_clone_tmp_both_move_to_scratch() -> None:
    """AC3. Two env vars, because the two consumers read two different names.

    ``REBAR_GATE_TMPDIR`` moves the content-addressed snapshot store; ``TMPDIR`` moves the
    per-review ``reviewbot-*`` clone, which ``voter.py`` creates through
    ``tempfile.TemporaryDirectory`` and which therefore follows the system temp dir. Setting
    only one leaves the other on root, which is half a fix.
    """
    env = _review_bot_service()["environment"]
    assert env["REBAR_GATE_TMPDIR"] == SCRATCH_MOUNT
    assert env["TMPDIR"] == SCRATCH_MOUNT


def test_the_scratch_bind_carries_the_declaration_marker_into_the_container() -> None:
    """Binding the mount point alone would hide the root-side declaration marker.

    The declaration lives beside the mount point, so the bind is the PARENT directory. Bind
    only ``<mount>`` and the container sees an empty directory whether the volume is mounted
    or not — the guard would then never fire, which is worse than not having it.
    """
    volumes = _review_bot_service()["volumes"]
    parent = str(Path(SCRATCH_MOUNT).parent)
    assert any(str(v).startswith(f"{parent}:{parent}") for v in volumes), volumes


# ── observability: the metric behind the alarms ──────────────────────────────────────


def test_observability_publishes_the_scratch_mount_metrics() -> None:
    src = (REPO_ROOT / "infra" / "scripts" / "observability.sh").read_text()
    assert "GATE_SCRATCH_MOUNT" in src
    assert "gate_scratch_mounted" in src
    assert SCRATCH_MOUNT in src
