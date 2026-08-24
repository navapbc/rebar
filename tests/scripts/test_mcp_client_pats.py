"""MCP per-client bearer PAT credential handling (ticket exuberant-blockish-avians,
epic jira-reb-3527 "Enable MCP on AWS"; ADR 0104 §1 static verifier + per-client PATs).

The per-client bearer PATs the ``static`` verifier authenticates are treated like every
other box secret: SSM-sourced, materialized on-box, gitignored, NEVER committed (gotcha
f600 — the box ``.env`` is rsync-EXCLUDED, so a rotated SSM secret is materialized on-box
at deploy, not baked into the rsync'd tree).

Three oracles (the ACs):

1. GUARD — the real local client-credential config path is gitignored (``git check-ignore``
   returns it) and no tracked file in the tree carries a real-PAT pattern (negative control:
   the committed placeholder template passes; a planted real-looking secret is detected).
2. MATERIALIZE — ``fetch-secrets.sh`` materializes each per-client PAT from SSM into the
   0600 rsync-excluded env source (``.env`` as ``MCP_CLIENT_PAT_*``) and emits the tokens
   JSON the ``static`` verifier reads (``mcp-static-tokens.json``, ``token_env`` records —
   never a plaintext token, never the raw value in the tokens file). Driven by a STUBBED
   aws/SSM sink feeding a known value (no live AWS). The tokens file is ALWAYS created,
   even when a slot is blank (bug beb1), and a blank slot's record is OMITTED so
   ``_parse_static_record`` never sees an empty ``token_env``.
3. VERIFIER CONTRAST — the real ``StaticBearerVerifier`` over the materialized tokens file
   accepts a request bearing a materialized PAT and rejects an unknown token (→ 401)
   through the real ``_load_static_tokens`` read path.

Plus a runbook oracle: operator-driven rotation (re-materialize + restart the rebar-mcp
process so the init-time verifier re-reads; autodeploy no-ops on a value-only rotation).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._mcp_auth import StaticBearerVerifier, _load_static_tokens

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FETCH_SECRETS = _REPO_ROOT / "infra" / "scripts" / "fetch-secrets.sh"
_AUTODEPLOY = _REPO_ROOT / "infra" / "scripts" / "autodeploy.sh"
_GITIGNORE = _REPO_ROOT / ".gitignore"
_TEMPLATE = _REPO_ROOT / "mcp-clients.local.example.json"
_RUNBOOK = _REPO_ROOT / "infra" / "runbooks" / "mcp-client-pats.md"

# The gitignored render-target path (relative to repo root) an operator fills with real PATs.
_REAL_CRED_REL = "mcp-clients.local.json"
# The on-box tokens file (relative to repo root) the static verifier reads.
_TOKENS_FILE_REL = "infra/compose/mcp-static-tokens.json"

# Detects real-looking GitHub PATs. Built to NOT match placeholder tokens (which use an
# obvious REPLACE_WITH_* / <...> shape) nor this file's own bracketed regex source.
_REAL_PAT_RE = re.compile(
    r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b|\bgithub_pat_[A-Za-z0-9_]{40,}\b"
)

# A known PAT value the stubbed SSM sink feeds; built by concatenation so no real-PAT
# literal ever appears in this source tree (which oracle #1 scans).
_KNOWN_COPILOT_PAT = "ghp_" + "C" * 36
_KNOWN_CODEX_PAT = "ghp_" + "D" * 36
_KNOWN_CLAUDE_PAT = "ghp_" + "E" * 36


def _git(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=_REPO_ROOT, capture_output=True, text=True, check=False
    )


# ─────────────────────────────── oracle 1: committed-secret guard ───────────


def test_guard_real_credential_path_is_gitignored() -> None:
    """The real local client-credential config path is matched by .gitignore."""
    res = _git(["check-ignore", "--", _REAL_CRED_REL])
    assert res.returncode == 0 and res.stdout.strip().endswith(_REAL_CRED_REL), (
        f"{_REAL_CRED_REL!r} must be gitignored so an operator's real PATs are never "
        f"committed; git check-ignore returned rc={res.returncode} out={res.stdout!r}"
    )
    # The committed template must exist and hold ONLY a placeholder (no real PAT).
    assert _TEMPLATE.exists(), f"committed placeholder template {_TEMPLATE} must exist"
    assert not _REAL_PAT_RE.search(_TEMPLATE.read_text(encoding="utf-8")), (
        "the committed template must carry a placeholder, never a real-looking PAT"
    )


def test_guard_detector_negative_and_positive_control() -> None:
    """The detector passes a placeholder (negative control) and catches a planted secret."""
    assert not _REAL_PAT_RE.search("Bearer REPLACE_WITH_COPILOT_PAT")
    assert not _REAL_PAT_RE.search('"Authorization": "Bearer <copilot-pat>"')
    planted = "ghp_" + "A" * 40  # a planted real-looking secret
    assert _REAL_PAT_RE.search(f'"token": "{planted}"'), "detector must catch a real PAT"


def test_guard_no_real_pat_in_tracked_tree() -> None:
    """No tracked file in the tree contains a real-PAT pattern."""
    tracked = _git(["ls-files"]).stdout.splitlines()
    offenders: list[str] = []
    for rel in tracked:
        p = _REPO_ROOT / rel
        try:
            text = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _REAL_PAT_RE.search(text):
            offenders.append(rel)
    assert not offenders, f"real-PAT pattern found in tracked files: {offenders}"


# ─────────────────────────────── fetch-secrets stubs ────────────────────────


def _write_stub(bin_dir: Path, name: str, body: str) -> None:
    p = bin_dir / name
    p.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    p.chmod(0o755)


def _run_fetch_secrets(tmp_path: Path, pats: dict[str, str]) -> tuple[Path, Path]:
    """Run fetch-secrets.sh under stubbed aws/curl. ``pats`` maps client->value; a missing
    or empty value simulates a blank SSM slot. Returns (env_file, tokens_file)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    compose = tmp_path / "compose"
    compose.mkdir(parents=True, exist_ok=True)
    env_file = compose / ".env"
    tokens_file = compose / "mcp-static-tokens.json"

    # curl stub: satisfy the IMDSv2 token + region reads (fetch-secrets uses curl -sf).
    _write_stub(
        bin_dir,
        "curl",
        """
        case "$*" in
          *api/token*)        echo "imds-token" ;;
          *placement/region*) echo "us-east-1" ;;
          *) exit 0 ;;
        esac
        """,
    )
    # aws stub: echo Parameter.Value for the requested --name leaf. Required leaves get a
    # non-empty stub; the three mcp-client-pat leaves get the (possibly blank) test values.
    _write_stub(
        bin_dir,
        "aws",
        """
        name=""
        args=("$@")
        for ((i=0; i<${#args[@]}; i++)); do
          [ "${args[i]}" = "--name" ] && name="${args[i+1]}"
        done
        case "$name" in
          */mcp-client-pat-copilot) printf '%s' "${STUB_COPILOT:-None}" ;;
          */mcp-client-pat-codex)   printf '%s' "${STUB_CODEX:-None}" ;;
          */mcp-client-pat-claude)  printf '%s' "${STUB_CLAUDE:-None}" ;;
          *) printf 'stub-value' ;;
        esac
        """,
    )

    env = subprocess_env(
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        ENV_FILE=str(env_file),
        STUB_COPILOT=pats.get("copilot", ""),
        STUB_CODEX=pats.get("codex", ""),
        STUB_CLAUDE=pats.get("claude", ""),
    )

    res = subprocess.run(
        ["bash", str(_FETCH_SECRETS)],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, f"fetch-secrets.sh failed: rc={res.returncode}\n{res.stderr}"
    return env_file, tokens_file


# ─────────────────────────────── oracle 2: materialization ──────────────────


def test_materialize_pat_into_env_and_tokens_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch-secrets materializes the raw PAT into the .env env source and emits a
    token_env-referencing tokens JSON the verifier can load — no plaintext token, no raw
    value in the tokens file."""
    env_file, tokens_file = _run_fetch_secrets(
        tmp_path,
        {"copilot": _KNOWN_COPILOT_PAT, "codex": _KNOWN_CODEX_PAT, "claude": _KNOWN_CLAUDE_PAT},
    )

    env_text = env_file.read_text(encoding="utf-8")
    assert f"MCP_CLIENT_PAT_COPILOT={_KNOWN_COPILOT_PAT}" in env_text, (
        "the raw PAT must be materialized into the 0600 rsync-excluded env source"
    )
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"

    doc = json.loads(tokens_file.read_text(encoding="utf-8"))
    records = doc["tokens"]
    assert {r["client_id"] for r in records} == {"copilot", "codex", "claude"}
    for r in records:
        assert "token" not in r, "tokens file must never carry a plaintext token"
        assert "token_sha256" not in r, "this design references env-var NAMES (token_env)"
        assert r["token_env"].startswith("MCP_CLIENT_PAT_")
    # The tokens file must NOT contain the raw PAT value anywhere.
    assert _KNOWN_COPILOT_PAT not in tokens_file.read_text(encoding="utf-8")

    # It loads via the REAL parser once the env vars are present.
    monkeypatch.setenv("MCP_CLIENT_PAT_COPILOT", _KNOWN_COPILOT_PAT)
    monkeypatch.setenv("MCP_CLIENT_PAT_CODEX", _KNOWN_CODEX_PAT)
    monkeypatch.setenv("MCP_CLIENT_PAT_CLAUDE", _KNOWN_CLAUDE_PAT)
    by_digest = _load_static_tokens(str(tokens_file))
    assert {v["client_id"] for v in by_digest.values()} == {"copilot", "codex", "claude"}


def test_materialize_blank_slot_omits_record_but_file_exists(tmp_path: Path) -> None:
    """beb1: the tokens file is ALWAYS created; a blank slot's record is OMITTED so the
    verifier parser never sees an empty token_env; all-blank yields an empty token set."""
    # One populated, two blank.
    _env_file, tokens_file = _run_fetch_secrets(tmp_path, {"copilot": _KNOWN_COPILOT_PAT})
    doc = json.loads(tokens_file.read_text(encoding="utf-8"))
    assert [r["client_id"] for r in doc["tokens"]] == ["copilot"], (
        "only populated clients get a token_env record"
    )

    # All blank → file still exists and is valid JSON with an empty token set (beb1).
    _, tokens_file2 = _run_fetch_secrets(tmp_path / "allblank", {})
    assert tokens_file2.exists(), "tokens file must ALWAYS be created (beb1)"
    assert json.loads(tokens_file2.read_text(encoding="utf-8"))["tokens"] == []


def test_materialized_artifacts_are_gitignored_and_rsync_protected() -> None:
    """The materialized env + tokens file are gitignored (NOT a committed source artifact)
    and the tokens file is excluded from autodeploy's rsync --delete."""
    for rel in ("infra/compose/.env", _TOKENS_FILE_REL):
        res = _git(["check-ignore", "--", rel])
        assert res.returncode == 0, f"{rel!r} must be gitignored (never committed)"
    autodeploy = _AUTODEPLOY.read_text(encoding="utf-8")
    assert _TOKENS_FILE_REL in autodeploy, (
        "autodeploy RSYNC_EXCLUDES must exclude the materialized tokens file so a "
        "value-only re-materialize is not clobbered by rsync --delete"
    )


# ─────────────────────────────── oracle 3: verifier contrast ────────────────


def test_verifier_accepts_materialized_pat_rejects_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real StaticBearerVerifier over a token_env tokens file accepts the materialized
    PAT and rejects an unknown token (→ 401)."""
    tokens_file = tmp_path / "mcp-static-tokens.json"
    tokens_file.write_text(
        json.dumps(
            {
                "tokens": [
                    {
                        "name": "copilot",
                        "client_id": "copilot",
                        "scopes": [],
                        "token_env": "MCP_CLIENT_PAT_COPILOT",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("MCP_CLIENT_PAT_COPILOT", _KNOWN_COPILOT_PAT)
    verifier = StaticBearerVerifier(
        tokens_file=str(tokens_file), resource="https://mcp.example.com"
    )
    good = asyncio.run(verifier.verify_token(_KNOWN_COPILOT_PAT))
    assert good is not None and good.client_id == "copilot"
    bad = asyncio.run(verifier.verify_token("ghp_" + "Z" * 36))
    assert bad is None, "an unknown token must be rejected (→ 401)"


# ─────────────────────────────── runbook oracle ─────────────────────────────


def test_runbook_documents_operator_driven_rotation() -> None:
    """The runbook documents re-materialize + restart (reload = process restart, since the
    verifier reads the tokens file only at init) and that autodeploy no-ops on a value-only
    rotation."""
    assert _RUNBOOK.exists(), f"rotation runbook {_RUNBOOK} must exist"
    text = _RUNBOOK.read_text(encoding="utf-8").lower()
    assert "rotat" in text
    assert "re-materialize" in text or "rematerialize" in text or "materialize" in text
    assert "restart" in text or "replace" in text or "blue-green" in text, (
        "reload must mean restarting/replacing the rebar-mcp process, not a file-only rewrite"
    )
    assert "autodeploy" in text, "the runbook must mention autodeploy behavior on rotation"
    assert "value-only" in text or "value only" in text, (
        "the runbook must state autodeploy no-ops on a value-only rotation (f600)"
    )
