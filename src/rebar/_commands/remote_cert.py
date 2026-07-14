"""``rebar remote-cert <ticket-id> <kind>`` — request a trusted-environment op-cert (story ee0b).

Routes a gate run to the trusted op-cert service at ``verify.opcert_remote_url``: SigV4-signs the
request with ambient AWS credentials, submits the async job, polls to a terminal status, and on a
``PASS`` verdict PERSISTS the returned envelope + its bound fields as a ``SIGNATURE`` event (the
SAME record shape ``signing.sign_opcert_manifest`` appends; auto-pushed with the tickets branch).
This is safe because the envelope is self-authenticating — tampering with a bound value breaks the
signature the merge gate (``rebar verify-opcert``) verifies. A non-PASS / error verdict exits
non-zero. The remote path is entirely opt-in: unset ``verify.opcert_remote_url`` → a clear error,
and NO local op-cert sign/verify path ever depends on it.
"""

from __future__ import annotations

import argparse
import sys
import time

from rebar import config

_USAGE = "rebar remote-cert <ticket-id> {completion-verifier|plan-review} [--root <path>]"
_VALID_KINDS = ("completion-verifier", "plan-review")
#: Client-side polling budget (the service enforces its own per-run timeout; this bounds the wait).
_POLL_INTERVAL_SECONDS = 2.0
_POLL_TIMEOUT_SECONDS = 1200.0


def cli(argv: list[str]) -> int:
    p = argparse.ArgumentParser(prog="rebar remote-cert", usage=_USAGE, description=__doc__)
    p.add_argument("ticket_id", help="the ticket to certify")
    p.add_argument("kind", choices=_VALID_KINDS, help="the gate kind to run")
    p.add_argument("--root", help="repo root (default: cwd)")
    args = p.parse_args(argv)

    try:
        cfg = config.load_config(root=args.root)
    except config.ConfigError as exc:
        print(f"remote-cert: {exc}", file=sys.stderr)
        return 2

    base_url = cfg.verify.opcert_remote_url
    if not base_url:
        print(
            "remote-cert: verify.opcert_remote_url is not set — no trusted op-cert service is "
            "configured. Set it in rebar.toml (or -c verify.opcert_remote_url=<url>) to use the "
            "remote gate; local op-cert operations never require it.",
            file=sys.stderr,
        )
        return 2

    try:
        job = submit_and_poll(base_url, args.ticket_id, args.kind)
    except Exception as exc:  # noqa: BLE001 — a transport/service failure is a clean non-zero exit
        print(f"remote-cert: request to {base_url} failed: {exc}", file=sys.stderr)
        return 1

    return finalize(job, args.ticket_id, args.kind, repo_root=args.root)


def finalize(job: dict, ticket_id: str, kind: str, *, repo_root=None) -> int:
    """Act on a terminal job record: persist the envelope on PASS (exit 0), else exit non-zero."""
    status = job.get("status")
    verdict = str(job.get("verdict") or "").upper()
    if status == "error":
        err = job.get("error") or {}
        print(
            f"remote-cert: gate errored ({err.get('class', 'internal')}: "
            f"{err.get('message', 'no detail')})",
            file=sys.stderr,
        )
        return 1
    if verdict != "PASS" or not job.get("envelope"):
        print(
            f"remote-cert: gate did not PASS (verdict={verdict or 'none'}); no op-cert persisted.",
            file=sys.stderr,
        )
        return 1

    resolved = persist_envelope(job, ticket_id, kind, repo_root=repo_root)
    print(f"remote-cert: persisted {kind} op-cert for {resolved}")
    return 0


def persist_envelope(job: dict, ticket_id: str, kind: str, *, repo_root=None) -> str:
    """Append the returned envelope + bound fields as a ``SIGNATURE`` event (the same record shape
    ``sign_opcert_manifest`` writes), so ``verify_signature`` / ``rebar verify-opcert`` certify it.
    Returns the resolved ticket id."""
    from rebar._commands._seam import (
        CommandError,
        append_event,
        require_id,
        require_not_ghost,
        tracker_dir,
    )
    from rebar.signing import SigningError

    tracker = tracker_dir(repo_root)
    try:
        resolved = require_id(ticket_id, tracker)
        require_not_ghost(resolved, tracker)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None

    manifest = job.get("manifest")
    if not isinstance(manifest, list) or not manifest:
        # Fall back to a minimal kind-prefixed manifest so the reducer files the record under
        # attestations[kind]; the security binding lives in the envelope, not the manifest.
        manifest = [f"{kind}: PASS", f"ticket: {resolved}"]
    record = {
        "manifest": manifest,
        "algorithm": "sshsig",
        "envelope": job["envelope"],
        "material_fingerprint": job.get("material_fingerprint"),
        "merged_log_commit": job.get("merged_log_commit"),
        "signed_at": time.time_ns(),
        "kind": kind,
    }
    try:
        append_event(resolved, "SIGNATURE", record, tracker, repo_root=repo_root)
    except CommandError as exc:
        raise SigningError(exc.message, exc.returncode) from None
    return resolved


def submit_and_poll(base_url: str, ticket_id: str, kind: str) -> dict:
    """Submit the job (SigV4-signed) and poll ``GET /opcert/jobs/{id}`` to a terminal status.

    Only ``{ticket_id, kind}`` is sent — the server derives every authoritative value itself.
    """
    import json

    submit = _sigv4_request(
        base_url,
        "POST",
        "/opcert/jobs",
        body=json.dumps({"ticket_id": ticket_id, "kind": kind}).encode("utf-8"),
    )
    job_id = submit.get("job_id")
    if not job_id:
        raise RuntimeError(f"service did not return a job_id (got {submit!r})")

    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while True:
        rec = _sigv4_request(base_url, "GET", f"/opcert/jobs/{job_id}")
        if rec.get("status") in ("completed", "error"):
            return rec
        if time.monotonic() > deadline:
            raise TimeoutError(f"job {job_id} did not reach a terminal status in time")
        time.sleep(_POLL_INTERVAL_SECONDS)


def _sigv4_request(base_url: str, method: str, path: str, *, body: bytes | None = None) -> dict:
    """Issue a SigV4-signed request to the API Gateway-fronted service and return the JSON body.

    ``botocore`` (SigV4 signing) is imported lazily so the client module stays importable without
    the signing stack; the deploy environment that actually calls the remote provides it."""
    import json
    import urllib.request

    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    from botocore.session import Session

    url = base_url.rstrip("/") + path
    session = Session()
    credentials = session.get_credentials()
    if credentials is None:
        raise RuntimeError("no ambient AWS credentials available to SigV4-sign the request")
    region = session.get_config_variable("region") or "us-east-1"

    headers = {"Content-Type": "application/json"} if body is not None else {}
    aws_req = AWSRequest(method=method, url=url, data=body, headers=headers)
    # "execute-api" is the service name for API Gateway SigV4.
    SigV4Auth(credentials, "execute-api", region).add_auth(aws_req)

    http_req = urllib.request.Request(
        url, data=body, method=method, headers=dict(aws_req.headers.items())
    )
    with urllib.request.urlopen(http_req, timeout=30) as resp:  # noqa: S310 — configured https URL
        return json.loads(resp.read().decode("utf-8"))
