"""Enforceable MCP-minted op-certs (story spoiled-bionic-goose / epic jira-reb-3527).

The on-box ``rebar-mcp`` server signs the op-certs its certified-op tools mint under the box's
EXISTING trusted signing environment (env_id + materialized Ed25519 key), so a cert it produces
verifies against the pinned public key — and the ``rebar verify-opcert`` merge-gate can enforce
that ONLY post-deploy closes carry such a cert while grandfathering historical closes.

These tests exercise the REAL crypto + verify paths (no stubs), against a FIXTURE ``rebar.toml``
and a temp store — NEVER the authoritative repo config (the live enforcement flip is a deferred,
human-run operator step; see ``infra/runbooks/mcp-opcert-enforcement-flip.md``). The mechanism
proven here is what makes that flip safe:

* AC1 — a cert minted through the server's startup signing binding
  (:func:`rebar.mcp_server.compose_startup_opcert_binding` + the context-bound signing seam)
  VERIFIES against the pinned pubkey through the real ``verify-opcert`` walk (positive), while an
  otherwise-identical cert signed by an UNREGISTERED key is REJECTED (contrast). No new pin is
  added and no private key is committed.
* AC2 — with ``verify.require_environment`` + ``verify.opcert_enforce_since`` set in the FIXTURE
  config, a post-boundary close without a valid completion-verifier cert FAILS (exit 1) while a
  pre-boundary close is grandfathered (exit 0).
* AC3 — unsetting ``verify.require_environment`` returns the gate to advisory everywhere (exit 0),
  and an existing pinned-env op-cert still verifies (regression oracle).
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import rebar
from rebar import signing
from rebar._mcp_health import run_http_with_grace, run_mcp
from rebar._opcert_binding import bound_signer, current_binding, current_push_mode
from rebar.attest import sshsig
from rebar.mcp_server import compose_startup_opcert_binding
from rebar.opcert_service.keyprov import OpcertKeyError

try:
    sshsig.ensure_available()
    _SSH_OK = True
except Exception:  # noqa: BLE001
    _SSH_OK = False

pytestmark = pytest.mark.skipif(not _SSH_OK, reason="ssh-keygen >= 8.9 required for SSHSIG")

# The box environment's identity — a UUID env_id (the compose service's REBAR_OPCERT_ENV_ID);
# the string is a test value, NOT the real prod env_id (whose private key lives only in SSM).
ENV_ID = "9f1c8e42-7a3b-4d5e-b6c1-2f0a9d8e7c65"
KIND = "completion-verifier"


def _keypair(tmp_path: Path, name: str) -> tuple[str, str]:
    key = tmp_path / name
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-q", "-C", name],
        check=True,
        capture_output=True,
    )
    key.chmod(0o600)
    parts = (tmp_path / f"{name}.pub").read_text().strip().split()
    return str(key), f"{parts[0]} {parts[1]}"


def _store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "9" * 18)
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "d@e.test"),
        ("git", "config", "user.name", "D"),
        ("git", "commit", "-q", "--allow-empty", "-m", "init"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    monkeypatch.delenv("REBAR_OPCERT_ENV_ID", raising=False)
    monkeypatch.delenv("REBAR_IDENTITY_SIGNING_KEY", raising=False)
    monkeypatch.delenv("REBAR_OPCERT_KEY_PATH", raising=False)
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tracker(repo: Path) -> str:
    from rebar._commands._seam import tracker_dir

    return str(tracker_dir(str(repo)))


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _tracker_head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=_tracker(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


def _tip_position(repo: Path) -> str:
    from rebar._store.ticket_layout import iter_ticket_dirs
    from rebar.reducer._cache import is_active_event

    tracker = _tracker(repo)
    best = ""
    for entry in iter_ticket_dirs(tracker):
        for path in Path(entry.path).iterdir():
            fn = path.name
            if not fn.endswith(".json") or fn.startswith(".") or not is_active_event(fn):
                continue
            pos = fn[:-5].rsplit("-", 1)[0]
            if pos > best:
                best = pos
    return best


def _write_trusted_env(repo: Path, env_id: str, pub: str, added_at_position: str) -> None:
    d = repo / ".rebar"
    d.mkdir(exist_ok=True)
    (d / "trusted_environments.yaml").write_text(
        "environments:\n"
        f"  - env_id: {env_id}\n"
        "    keys:\n"
        f"      - public_key: {pub}\n"
        f"        added_at_log_position: {added_at_position}\n"
        "        revoked_at_log_position: null\n",
        encoding="utf-8",
    )


def _write_verify_config(
    repo: Path, *, require_environment=None, opcert_enforce_since=None
) -> None:
    """Write a FIXTURE rebar.toml [verify] posture — never the authoritative repo config."""
    lines = ["[verify]\n"]
    if require_environment is not None:
        lines.append(f'require_environment = "{require_environment}"\n')
    if opcert_enforce_since is not None:
        lines.append(f'opcert_enforce_since = "{opcert_enforce_since}"\n')
    (repo / "rebar.toml").write_text("".join(lines), encoding="utf-8")


def _run_verify_opcert(repo: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rebar", "verify-opcert", "--root", str(repo), *extra],
        capture_output=True,
        text=True,
        check=False,
    )


def _compose_binding(repo: Path, monkeypatch, env_id: str, key_path: str):
    """Build the server startup binding EXACTLY as the box does: REBAR_OPCERT_ENV_ID +
    REBAR_IDENTITY_SIGNING_KEY (the materialized key path) → compose_startup_opcert_binding."""
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", env_id)
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", key_path)
    cfg = rebar.config.compose_config(root=str(repo))
    return compose_startup_opcert_binding(cfg)


def _mint_completion_cert_via_binding(repo: Path, tid: str, binding, commit: str) -> None:
    """Mint + persist a completion-verifier op-cert through the SAME context-bound signing seam
    the certified-op MCP tools use (``signer=None`` → the context binding resolves the key +
    principal), proving the server's startup binding is what signs the cert."""
    from rebar.llm.plan_review.attest import current_material_fingerprint

    material = current_material_fingerprint(tid, repo_root=str(repo))
    manifest = [f"{KIND}: PASS", f"material: {material}", signing.verified_at_sha_step(commit)]
    with bound_signer(binding, push_mode=None):
        signing.sign_manifest(tid, manifest, kind=KIND, repo_root=str(repo))


# ── AC1: round-trip against the pinned environment ─────────────────────────────────────────────
def test_startup_binding_composes_from_env_and_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server composes a signer from REBAR_OPCERT_ENV_ID + the materialized key: the binding's
    principal is the env_id and its key is a process-owned copy (never the bind-mount source)."""
    repo = _store(tmp_path, monkeypatch)
    priv, _pub = _keypair(tmp_path, "boxkey")
    binding = _compose_binding(repo, monkeypatch, ENV_ID, priv)
    assert binding is not None
    assert binding.principal == ENV_ID
    assert binding.key_path != priv  # a 0600 process-owned copy, not the source
    assert Path(binding.key_path).exists()
    binding.cleanup()


def test_unprovisioned_startup_binding_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No env_id (the dev / stdio default) → no binding: signing keeps its env/genesis path."""
    repo = _store(tmp_path, monkeypatch)
    cfg = rebar.config.compose_config(root=str(repo))
    assert compose_startup_opcert_binding(cfg) is None


def test_container_minted_cert_verifies_against_pinned_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 positive: a completion-verifier cert minted through the server's startup binding
    verifies against the pinned pubkey through the real verify-opcert walk (exit 0)."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "boxkey")
    commit = _head(repo)
    tid = rebar.create_ticket("task", "gated work", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))

    binding = _compose_binding(repo, monkeypatch, ENV_ID, priv)
    assert binding is not None
    with bound_signer(binding, push_mode=None):
        # The context binding IS what the mint seam resolves (the container's signing identity).
        assert signing.ensure_opcert_key(_tracker(repo)) == binding.key_path
        assert signing.opcert_principal(_tracker(repo)) == ENV_ID
    _mint_completion_cert_via_binding(repo, tid, binding, commit)
    binding.cleanup()
    rebar.transition(tid, "open", "closed", repo_root=str(repo))

    proc = _run_verify_opcert(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_unregistered_key_cert_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """AC1 contrast: an otherwise-identical cert minted under an UNREGISTERED key (a binding whose
    key is not the pinned one) is REJECTED by the real verify path (exit 1)."""
    repo = _store(tmp_path, monkeypatch)
    _pinned_priv, pinned_pub = _keypair(tmp_path, "pinned")
    foreign_priv, _foreign_pub = _keypair(tmp_path, "foreign")
    commit = _head(repo)
    tid = rebar.create_ticket("task", "gated work", repo_root=str(repo))
    # Pin the TRUSTED key, but mint the cert under a FOREIGN key composed the same way.
    _write_trusted_env(repo, ENV_ID, pinned_pub, _tip_position(repo))

    binding = _compose_binding(repo, monkeypatch, ENV_ID, foreign_priv)
    assert binding is not None
    _mint_completion_cert_via_binding(repo, tid, binding, commit)
    binding.cleanup()
    rebar.transition(tid, "open", "closed", repo_root=str(repo))

    proc = _run_verify_opcert(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    # Negative-control oracle: the exit-1 must be the cert REJECTION (untrusted signer), not an
    # unrelated infra fault (exit 2) or crash — assert the specific rejection reason is reported.
    combined = proc.stdout + proc.stderr
    assert "invalid op-cert" in combined, f"stdout={proc.stdout}\nstderr={proc.stderr}"
    assert tid in combined


def test_no_private_key_is_committed_in_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC1 negative control: the round-trip persists a cert + a trusted-env PUBLIC pin, but never
    a private key. The materialized key copy lives OUTSIDE the tracked store."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "boxkey")
    commit = _head(repo)
    tid = rebar.create_ticket("task", "gated work", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    binding = _compose_binding(repo, monkeypatch, ENV_ID, priv)
    _mint_completion_cert_via_binding(repo, tid, binding, commit)
    key_copy = binding.key_path
    binding.cleanup()

    # The private-key copy is not inside the tracker/store tree.
    assert _tracker(repo) not in key_copy
    assert str(repo) not in key_copy
    # No OpenSSH private key material anywhere under the tracker.
    for p in Path(_tracker(repo)).rglob("*"):
        if p.is_file():
            body = p.read_bytes()
            assert b"BEGIN OPENSSH PRIVATE KEY" not in body, f"private key leaked into {p}"


# ── AC2: enforce post-boundary, grandfather pre-boundary (via FIXTURE config) ───────────────────
def test_post_boundary_close_without_cert_is_enforced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 enforced: with require_environment + opcert_enforce_since in the FIXTURE rebar.toml, a
    ticket closed AFTER the boundary without a valid completion-verifier cert FAILS (exit 1)."""
    repo = _store(tmp_path, monkeypatch)
    _priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "post-boundary work", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    # Boundary = the tracker tip BEFORE the close, so the close-STATUS commit is a DESCENDANT
    # of it → enforced.
    boundary = _tracker_head(repo)
    rebar.transition(tid, "open", "closed", repo_root=str(repo))  # closed, NO op-cert
    _write_verify_config(repo, require_environment=ENV_ID, opcert_enforce_since=boundary)

    proc = _run_verify_opcert(repo)  # no CLI flags → posture comes from the fixture config
    assert proc.returncode == 1, f"stdout={proc.stdout}\nstderr={proc.stderr}"


def test_pre_boundary_close_without_cert_is_grandfathered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC2 grandfathered: a ticket closed BEFORE the boundary without a cert is advisory (exit 0),
    even though the SAME require_environment posture is active in the fixture config."""
    repo = _store(tmp_path, monkeypatch)
    _priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "historical work", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    rebar.transition(tid, "open", "closed", repo_root=str(repo))  # closed, NO op-cert
    # A LATER tracker commit becomes the boundary, so the close predates it → grandfathered.
    rebar.create_ticket("task", "later activity", repo_root=str(repo))
    boundary = _tracker_head(repo)
    _write_verify_config(repo, require_environment=ENV_ID, opcert_enforce_since=boundary)

    proc = _run_verify_opcert(repo)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# ── AC3: rollback is a config revert ────────────────────────────────────────────────────────────
def test_rollback_unset_require_environment_is_advisory_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3: unsetting require_environment (fixture config) → advisory everywhere (exit 0), even for
    a post-boundary close that WOULD have been enforced when require_environment was set."""
    repo = _store(tmp_path, monkeypatch)
    _priv, pub = _keypair(tmp_path, "env")
    tid = rebar.create_ticket("task", "post-boundary work", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    boundary = _tracker_head(repo)
    rebar.transition(tid, "open", "closed", repo_root=str(repo))  # closed, NO op-cert

    # Sanity: with the environment required, this post-boundary close is enforced → exit 1.
    _write_verify_config(repo, require_environment=ENV_ID, opcert_enforce_since=boundary)
    enforced = _run_verify_opcert(repo)
    assert enforced.returncode == 1, f"stdout={enforced.stdout}\nstderr={enforced.stderr}"

    # Rollback: unset require_environment (leave the boundary) → advisory everywhere → exit 0.
    _write_verify_config(repo, opcert_enforce_since=boundary)
    rolled_back = _run_verify_opcert(repo)
    assert rolled_back.returncode == 0, f"stdout={rolled_back.stdout}\nstderr={rolled_back.stderr}"


def test_rollback_existing_opcert_trust_still_verifies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC3 regression oracle: after rollback the existing pinned-env op-cert trust path is intact —
    re-enabling require_environment still certifies a validly signed cert (exit 0)."""
    repo = _store(tmp_path, monkeypatch)
    priv, pub = _keypair(tmp_path, "env")
    commit = _head(repo)
    tid = rebar.create_ticket("task", "gated work", repo_root=str(repo))
    _write_trusted_env(repo, ENV_ID, pub, _tip_position(repo))
    binding = _compose_binding(repo, monkeypatch, ENV_ID, priv)
    _mint_completion_cert_via_binding(repo, tid, binding, commit)
    binding.cleanup()
    rebar.transition(tid, "open", "closed", repo_root=str(repo))

    # Rollback posture (no required env) → advisory → exit 0.
    _write_verify_config(repo)
    assert _run_verify_opcert(repo).returncode == 0
    # Re-enabling the environment: the existing cert still verifies (trust path unchanged).
    proc = _run_verify_opcert(repo, "--require-environment", ENV_ID)
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"


# ── Binding wiring: push policy + serving-thread threading (move-1 mechanism guards) ────────────
def test_bound_signer_none_push_mode_falls_back_to_env_config() -> None:
    """The MCP path binds the signer with ``push_mode=None`` so the box STILL auto-pushes its
    ticket writes: ``current_push_mode()`` must stay None (env/config fallback), NOT be forced to
    'off' like the store-read-only gate service. A concrete mode is still honored (contrast)."""
    fake = SimpleNamespace(key_path="/proc/owned/copy", principal=ENV_ID)
    with bound_signer(fake, push_mode=None):
        # Signer IS bound (certs sign under the box env) ...
        assert current_binding() is fake
        # ... but the push policy falls back to env/config (None), so the box auto-pushes.
        assert current_push_mode() is None
    # Contrast: the gate service's default binds 'off', proving None is not silently coerced.
    with bound_signer(fake, push_mode="off"):
        assert current_push_mode() == "off"
    # Cleanly reset on exit.
    assert current_binding() is None
    assert current_push_mode() is None


def test_run_mcp_stdio_binds_signer_around_serving() -> None:
    """``run_mcp`` (stdio) must enter the binding AROUND ``server.run`` so the certified-op tools
    mint under the box env: the fake server observes the binding active while it 'serves', and it
    is reset afterward."""
    fake_binding = SimpleNamespace(key_path="/proc/owned/copy", principal=ENV_ID)
    seen: dict[str, object] = {}

    class _FakeServer:
        def run(self, *, transport: str) -> None:
            seen["transport"] = transport
            seen["binding"] = current_binding()
            seen["push_mode"] = current_push_mode()

    run_mcp(_FakeServer(), SimpleNamespace(transport="stdio"), opcert_binding=fake_binding)
    assert seen["transport"] == "stdio"
    assert seen["binding"] is fake_binding
    assert seen["push_mode"] is None
    # The caller context is not left bound.
    assert current_binding() is None


def test_run_http_binds_signer_inside_the_serving_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core threading claim: the binding is entered INSIDE the uvicorn serving thread, not the
    caller thread (a ContextVar set on the main thread is NOT inherited by a new thread). We fake
    uvicorn so ``Server.run`` records the thread it runs on and the binding it observes, then
    assert the binding was active there AND that thread differs from the caller's."""
    fake_binding = SimpleNamespace(key_path="/proc/owned/copy", principal=ENV_ID)
    seen: dict[str, object] = {}
    caller_thread = threading.get_ident()

    class _FakeConfig:
        def __init__(self, *a, **k) -> None:
            pass

    class _FakeUvicornServer:
        def __init__(self, _config: object) -> None:
            self.should_exit = False

        def run(self) -> None:
            seen["thread"] = threading.get_ident()
            seen["binding"] = current_binding()
            seen["push_mode"] = current_push_mode()

    monkeypatch.setattr("uvicorn.Config", _FakeConfig)
    monkeypatch.setattr("uvicorn.Server", _FakeUvicornServer)

    fake_mcp = SimpleNamespace(
        streamable_http_app=lambda: object(),
        settings=SimpleNamespace(host="127.0.0.1", port=0, log_level="INFO"),
    )
    from rebar._mcp_health import InFlightGauge

    # The caller thread must NOT be bound (proving the caller-thread binding would be wrong).
    assert current_binding() is None
    run_http_with_grace(fake_mcp, InFlightGauge(), opcert_binding=fake_binding)

    assert seen["binding"] is fake_binding, "signer not bound inside the serving thread"
    assert seen["push_mode"] is None
    assert seen["thread"] != caller_thread, "serving ran on the caller thread, not a worker thread"
    assert current_binding() is None


def test_compose_startup_binding_fails_closed_on_bad_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail-closed: with both inputs present but the key too-permissive (not 0600),
    ``compose_startup_opcert_binding`` RAISES (startup aborts) rather than returning None or
    serving with an unusable signer."""
    repo = _store(tmp_path, monkeypatch)
    priv, _pub = _keypair(tmp_path, "boxkey")
    Path(priv).chmod(0o644)  # group/other readable → rejected by the 0600 source check
    monkeypatch.setenv("REBAR_OPCERT_ENV_ID", ENV_ID)
    monkeypatch.setenv("REBAR_IDENTITY_SIGNING_KEY", priv)
    cfg = rebar.config.compose_config(root=str(repo))
    with pytest.raises(OpcertKeyError):
        compose_startup_opcert_binding(cfg)


def test_main_cleans_up_binding_when_serving_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """``main()``'s finally-block must ALWAYS release the process-owned key copy, even when serving
    raises: the composed binding's ``cleanup()`` is called on the serving-exception path."""
    import sys

    from rebar import mcp_server

    cleaned = {"count": 0}

    class _FakeBinding:
        key_path = "/proc/owned/copy"
        principal = ENV_ID

        def cleanup(self) -> None:
            cleaned["count"] += 1

    fake_cfg = SimpleNamespace(
        mcp=SimpleNamespace(transport="stdio"),
        identity=SimpleNamespace(signing_key=""),
    )
    monkeypatch.setattr(sys, "argv", ["rebar-mcp"])
    monkeypatch.setattr(rebar.config, "compose_config", lambda: fake_cfg)
    monkeypatch.setattr(mcp_server, "build_server", lambda cfg: object())
    monkeypatch.setattr(mcp_server, "compose_startup_opcert_binding", lambda cfg: _FakeBinding())

    def _boom(*a, **k):
        raise RuntimeError("serving failed")

    monkeypatch.setattr("rebar._mcp_health.run_mcp", _boom)

    with pytest.raises(RuntimeError, match="serving failed"):
        mcp_server.main()
    assert cleaned["count"] == 1, "binding.cleanup() not called on the serving-exception path"
