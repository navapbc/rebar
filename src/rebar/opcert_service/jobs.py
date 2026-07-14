"""The trusted op-cert gate job: authoritative fetch → per-kind dispatch → sign → read back.

This is the security core, and it is FastAPI-free on purpose (importable without the ``reviewbot``
extra; the async HTTP shell in ``app.py`` calls :func:`run_job` in a worker thread). The invariants:

* **Client inputs cannot influence bound values.** :func:`run_job` takes ONLY ``ticket_id`` +
  ``kind`` and derives every authoritative value from the ephemeral workspace it fetches itself
  (``merged_log_commit`` = the review remote's ``main`` tip; ``material_fingerprint`` recomputed
  from the fetched ticket state). The reported/bound fields are read back from the SIGNED
  envelope, so a doctored request field is simply never consulted.
* **The server never pushes.** The whole job runs with ``REBAR_SYNC_PUSH=off``, and the workspace
  has no git remote — a gate's ``sign=True`` SIGNATURE append lands only in the discarded clone.
* **Per-kind native verdicts, no unified enum.** ``plan-review`` → ``review_plan`` (PASS|BLOCK|
  INDETERMINATE; degrades in-band, never raises on an LLM outage → job COMPLETES). ``completion-
  verifier`` → ``verify_completion`` (PASS|FAIL; RAISES LLM errors → job ``error``). ``envelope``
  is non-null ONLY on PASS.
* **Signing is the producer seam, once.** ``review_plan`` signs internally; a completion PASS is
  signed via :func:`rebar._commands.transition_close.sign_completion_verdict` — the SAME producer
  the close gate uses. The key is the SSM-provisioned temp key (see :mod:`.keyprov`).
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Iterator

from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.keyprov import provisioned_signing_key
from rebar.opcert_service.ssm import SsmKeyFetcher
from rebar.opcert_service.workspace import Workspace, discard, prepare_workspace

#: The only kinds a client may request (validated before enqueue).
VALID_KINDS = ("completion-verifier", "plan-review")

#: The closed ``error.class`` enum, mapped from the raised exception type.
ERR_LLM_UNAVAILABLE = "llm_unavailable"
ERR_LLM_ERROR = "llm_error"
ERR_TIMEOUT = "timeout"
ERR_INTERNAL = "internal"


def new_record(job_id: str, ticket_id: str, kind: str) -> dict:
    """A fresh queued job record (the response/GET shape)."""
    return {
        "job_id": job_id,
        "ticket_id": ticket_id,
        "kind": kind,
        "status": "queued",
        "verdict": None,
        "envelope": None,
        "material_fingerprint": None,
        "merged_log_commit": None,
        "error": None,
    }


def run_job(
    *,
    ticket_id: str,
    kind: str,
    cfg: OpcertServiceConfig,
    ssm_fetcher: SsmKeyFetcher,
    review_plan_fn=None,
    verify_completion_fn=None,
    workspace_factory=None,
) -> dict:
    """Run one gate job to a terminal state; return the terminal record fields.

    NEVER raises for an expected failure (LLM outage/error, workspace/internal problem) — it maps
    those to ``status="error"`` with the closed ``error.class`` enum. ``review_plan_fn`` /
    ``verify_completion_fn`` / ``workspace_factory`` are test seams (defaults call the real LLM
    ops / fetch a real ephemeral clone); tests inject fakes so no network / AWS / LLM call runs.
    """
    factory = workspace_factory or prepare_workspace
    llm_error, llm_unavailable = _llm_error_classes()
    fields = new_record("", ticket_id, kind)
    fields.pop("job_id")
    ws: Workspace | None = None
    try:
        with _sync_push_off():
            ws = factory(cfg)
            fields["merged_log_commit"] = ws.merged_log_commit
            fields["material_fingerprint"] = _server_material(ticket_id, ws.repo_root)
            with provisioned_signing_key(cfg, ssm_fetcher):
                if kind == "plan-review":
                    _dispatch_plan_review(fields, ticket_id, ws, review_plan_fn)
                else:
                    _dispatch_completion(fields, ticket_id, ws, verify_completion_fn)
        fields["status"] = "completed"
    except llm_unavailable as exc:
        _mark_error(fields, ERR_LLM_UNAVAILABLE, exc)
    except llm_error as exc:
        _mark_error(fields, ERR_LLM_ERROR, exc)
    except Exception as exc:  # noqa: BLE001 — any other failure is an internal (never-client) error
        _mark_error(fields, ERR_INTERNAL, exc)
    finally:
        if ws is not None:
            discard(ws.repo_root)
    return fields


def _dispatch_completion(fields: dict, ticket_id: str, ws: Workspace, fn) -> None:
    """``completion-verifier``: run ``verify_completion`` (PASS|FAIL, RAISES on LLM error) and, on a
    PASS, sign the completion verdict through the shared producer seam, then read back the cert."""
    result = (fn or _default_verify_completion)(ticket_id, ws.repo_root)
    verdict = str(result.get("verdict", "")).upper()
    fields["verdict"] = verdict
    if verdict == "PASS":
        from rebar._commands.transition_close import sign_completion_verdict

        sign_completion_verdict(result, ticket_id, ws.repo_root)
        _attach_envelope(fields, ticket_id, ws.repo_root, "completion-verifier")


def _dispatch_plan_review(fields: dict, ticket_id: str, ws: Workspace, fn) -> None:
    """``plan-review``: run ``review_plan`` (PASS|BLOCK|INDETERMINATE; degrades in-band, signs
    internally on a non-blocking PASS), then read back the cert on a PASS."""
    result = (fn or _default_review_plan)(ticket_id, ws.repo_root)
    verdict = str(result.get("verdict", "")).upper()
    fields["verdict"] = verdict
    if verdict == "PASS":
        _attach_envelope(fields, ticket_id, ws.repo_root, "plan-review")


def _attach_envelope(fields: dict, ticket_id: str, repo_root: str, kind: str) -> None:
    """Read the freshly-appended op-cert back from the ephemeral store and attach the encoded
    envelope + its SIGNED bound fields (the authoritative, server-derived values) to ``fields``."""
    import rebar
    from rebar.attest import opcert

    state = rebar.show_ticket(ticket_id, repo_root=repo_root)
    record = (state.get("attestations") or {}).get(kind)
    if not isinstance(record, dict):
        raise RuntimeError(f"op-cert PASS produced no {kind!r} attestation to return")
    decoded = opcert.opcert_from_record(record)
    if decoded is None:
        raise RuntimeError(f"the {kind!r} attestation carries no decodable op-cert envelope")
    _envelope, bound = decoded
    fields["envelope"] = record.get("envelope")
    # Bind the reported values to the SIGNED payload (never the plaintext mirror, never the client).
    if bound.get("material_fingerprint") is not None:
        fields["material_fingerprint"] = bound["material_fingerprint"]
    if bound.get("merged_log_commit") is not None:
        fields["merged_log_commit"] = bound["merged_log_commit"]
    fields["manifest"] = record.get("manifest")


def _server_material(ticket_id: str, repo_root: str) -> str | None:
    """The material fingerprint the server derives itself from the fetched ticket state (the same
    one the gate binds). Best-effort — a PASS overwrites it with the signed envelope's value."""
    try:
        from rebar.llm.plan_review.attest import current_material_fingerprint

        return current_material_fingerprint(ticket_id, repo_root=repo_root)
    except Exception:  # noqa: BLE001 — non-fatal; the signed envelope is the authoritative source
        return None


def _default_verify_completion(ticket_id: str, repo_root: str) -> dict:
    from rebar import llm

    return llm.verify_completion(
        ticket_id, graph=False, source="attested", ref="HEAD", fetch=False, repo_root=repo_root
    )


def _default_review_plan(ticket_id: str, repo_root: str) -> dict:
    from rebar import llm

    return llm.review_plan(ticket_id, ref="HEAD", source="attested", repo_root=repo_root)


def _mark_error(fields: dict, cls: str, exc: BaseException) -> None:
    fields["status"] = "error"
    fields["verdict"] = None
    fields["envelope"] = None
    fields["error"] = {"class": cls, "message": str(exc)}


def _llm_error_classes() -> tuple[type[BaseException], type[BaseException]]:
    """(LLMError, LLMUnavailableError), or an unreachable sentinel pair when ``rebar.llm`` is
    unavailable (no ``[agents]`` extra) — so the except ladder is always well-formed."""
    try:
        from rebar.llm.errors import LLMError, LLMUnavailableError

        return LLMError, LLMUnavailableError
    except Exception:  # noqa: BLE001 — no agents extra: fall back to a never-raised sentinel

        class _NeverRaised(Exception):
            pass

        return _NeverRaised, _NeverRaised


@contextlib.contextmanager
def _sync_push_off() -> Iterator[None]:
    """Force ``REBAR_SYNC_PUSH=off`` for the job so no gate write is ever pushed."""
    prior = os.environ.get("REBAR_SYNC_PUSH")
    os.environ["REBAR_SYNC_PUSH"] = "off"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("REBAR_SYNC_PUSH", None)
        else:
            os.environ["REBAR_SYNC_PUSH"] = prior
