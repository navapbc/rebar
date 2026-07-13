"""Out-of-band trusted-environment key config + required-environment policy check (story 42d1).

A project pins the public keys of trusted signing environments **out-of-band** in
``.rebar/trusted_environments.yaml`` — on the code branch (Gerrit-gated + CODEOWNERS-protected),
**never** the auto-pushed tickets branch — and may require a gate's operation certificate to come
from a specific one of them. The required-environment check verifies the certificate against the
**pinned** key for that environment (via :func:`rebar.attest.opcert.verify_opcert`), never the
certificate's own self-claimed keyid (an unauthenticated hint).

Config schema (``.rebar/trusted_environments.yaml``)::

    environments:
      - env_id: "<string>"                  # MUST equal the DSSE principal (keyid)
        keys:
          - public_key: "ssh-ed25519 AAAA…" # one OpenSSH ed25519 authorized-keys line
            added_at_commit: "<git-sha>"     # era start (a commit on main)
            revoked_at_commit: "<git-sha>|null"

Loader posture mirrors ``rebar.llm.criteria.overlay``: **fail-open** when the file is absent (no
required environment — the low-security default), a **located** :class:`TrustedEnvError` (naming
the path) when the file is present but malformed.

API STUB — signatures + docstrings pinned for the RED oracle; bodies filled by the implementer.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from rebar.attest import dsse, opcert, registry

TRUSTED_ENV_FILENAME = "trusted_environments.yaml"


class TrustedEnvError(Exception):
    """A present-but-malformed ``.rebar/trusted_environments.yaml`` (located; names the path)."""


def _resolve_repo_root(repo_root: str | None) -> str | None:
    """The explicit arg, else the rebar project root (``config.repo_root()``); ``None`` when
    there is no resolvable root (mirrors ``rebar.llm.criteria.overlay._resolve_repo_root``)."""
    if repo_root is not None:
        return str(repo_root)
    try:
        from rebar import config as _config

        return str(_config.repo_root())
    except Exception:  # noqa: BLE001 — no repo ⇒ no pinned environments (fail-open)
        return None


def _config_path(repo_root: str | None) -> Path | None:
    if not repo_root:
        return None
    return Path(repo_root) / ".rebar" / TRUSTED_ENV_FILENAME


def load_trusted_environments(repo_root: str | None = None) -> dict | None:
    """Read + parse ``.rebar/trusted_environments.yaml``, or ``None`` when absent (fail-open).

    A present-but-unreadable/malformed file raises a located :class:`TrustedEnvError`.
    """
    path = _config_path(_resolve_repo_root(repo_root))
    if path is None or not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TrustedEnvError(f"cannot read trusted-environments config {path}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise TrustedEnvError(
            f"trusted-environments config {path} is not valid YAML: {exc}"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("environments"), list):
        raise TrustedEnvError(
            f"trusted-environments config {path} must be a mapping with an 'environments' list; "
            f"got {type(data).__name__}"
        )
    return data


def trusted_env_keyring(env_id: str, repo_root: str | None = None) -> list[dict] | None:
    """The pinned key records for ``env_id`` (``{public_key, added_at_commit, revoked_at_commit}``),
    or ``None`` when the config is absent or ``env_id`` is not pinned.
    """
    data = load_trusted_environments(repo_root)
    if data is None:
        return None
    for env in data.get("environments") or []:
        if isinstance(env, dict) and env.get("env_id") == env_id:
            keyring: list[dict] = []
            for key in env.get("keys") or []:
                keyring.append(
                    {
                        "public_key": key.get("public_key"),
                        "added_at_commit": key.get("added_at_commit"),
                        "revoked_at_commit": key.get("revoked_at_commit"),
                    }
                )
            return keyring
    return None


def verify_required_environment(
    envelope: dsse.Envelope,
    ticket_id: str,
    material_fingerprint: str,
    merged_log_commit: str,
    required_env_id: str,
    *,
    kind: str,
    repo_root: str | None = None,
) -> registry.Verdict:
    """Verify ``envelope`` is an op-cert from the pinned ``required_env_id`` for
    ``{ticket_id, material_fingerprint, merged_log_commit}``.

    Loads ``required_env_id``'s pinned keyring and delegates to
    :func:`rebar.attest.opcert.verify_opcert` with ``principal=required_env_id`` — so the signature
    is verified against the PINNED key, never the certificate's self-claimed keyid. A required
    environment that is not pinned fails (non-verified). Verdicts pass through from
    ``verify_opcert`` (``certified`` / ``mismatch`` / ``key_not_valid_at_era``).
    """
    keyring = trusted_env_keyring(required_env_id, repo_root)
    if keyring is None:
        return registry.Verdict(
            verified=False,
            verdict="mismatch",
            reason=(
                f"required environment {required_env_id} is not pinned in "
                ".rebar/trusted_environments.yaml"
            ),
        )
    return opcert.verify_opcert(
        envelope,
        ticket_id,
        material_fingerprint,
        merged_log_commit,
        keyring,
        kind=kind,
        principal=required_env_id,
        repo_root=repo_root,
    )
