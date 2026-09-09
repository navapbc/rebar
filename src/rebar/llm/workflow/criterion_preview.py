"""Preview a plan-review criterion against an isolated fixture.

LLM criteria run a Pass-1 finder; DET criteria scan a disposable repository.
Context-dependent container/G3/G4/ISF finders raise :class:`PreviewError`.
Synchronous previews are bounded, while :func:`preview_or_job` preserves timed-out
work for polling. Overlay authoring couples routing and activation atomically.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any

__all__ = [
    "PreviewError",
    "author_criterion_overlay",
    "handle_preview_post",
    "poll_job",
    "preview_criterion",
    "preview_or_job",
    "write_criterion_overlay",
]

# Container/ISF finders read the live ticket graph / a session log — not an inline fixture.
_NOT_PREVIEWABLE = frozenset({"G3", "G4", "ISF"})

# Language → file extension, for inferring a fixture filename a DET rule will accept.
_LANG_EXT = {
    "python": "py",
    "javascript": "js",
    "typescript": "ts",
    "tsx": "tsx",
    "jsx": "jsx",
    "go": "go",
    "java": "java",
    "ruby": "rb",
    "rust": "rs",
    "c": "c",
    "cpp": "cpp",
    "csharp": "cs",
    "php": "php",
    "yaml": "yaml",
    "json": "json",
    "bash": "sh",
    "shell": "sh",
    "sh": "sh",
}


class PreviewError(Exception):
    """A criterion cannot be previewed inline (unknown id, or a container/ISF finder that
    needs a ticket graph / session log). The editor handler maps it to an HTTP 4xx."""


def preview_criterion(
    request: dict[str, Any],
    *,
    repo_root: str | None,
    runner: Any = None,
    timeout: float = 60.0,
) -> dict[str, Any]:
    """Preview an existing or inline criterion.

    ``DET`` uses grounding; other tiers use Pass-1. Unknown or context-dependent
    criteria raise :class:`PreviewError`. Timeout returns a no-fire timed-out verdict.
    """
    # Bound the call so the editor stays responsive; preview_or_job retains a timed-out
    # worker for polling.
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_preview_core, request, repo_root=repo_root, runner=runner)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeout:
            return {
                "verdict": "no-fire",
                "finding": None,
                "rationale": (
                    f"preview timed out after {timeout:g}s "
                    "(spike-gate: fall back to async job+poll)"
                ),
                "timed_out": True,
            }


def _run_preview_core(
    request: dict[str, Any], *, repo_root: str | None, runner: Any
) -> dict[str, Any]:
    """Validate the request, resolve the criterion's routing, and dispatch DET vs LLM — WITHOUT
    any timeout wrapper (the caller owns the timeout/background policy). Raises
    :class:`PreviewError` for a bad request / unknown / container-ISF criterion."""
    criterion_id = request.get("criterion_id")
    inline = request.get("inline")
    fixture = request.get("fixture") or {}
    if not isinstance(fixture, dict):
        raise PreviewError("fixture must be an object {input, filename?, expect?}")

    if criterion_id:
        if criterion_id in _NOT_PREVIEWABLE:
            raise PreviewError(
                f"criterion {criterion_id!r} is a container/ISF finder (needs a ticket graph / "
                "session log); not previewable inline"
            )
        from rebar.llm.plan_review import registry

        routing = registry.effective_routing(repo_root).get(criterion_id)
        if routing is None:
            raise PreviewError(f"unknown criterion {criterion_id!r}")
    elif inline is not None:
        routing = (inline or {}).get("routing") or {}
        if not isinstance(routing, dict):
            raise PreviewError("inline.routing must be an object")
    else:
        raise PreviewError("request must supply either 'criterion_id' or 'inline'")

    exec_v = str(routing.get("exec", "")).upper()
    if exec_v == "DET":
        return _preview_det(criterion_id, routing, fixture, repo_root)
    return _preview_llm(criterion_id, inline, fixture, repo_root, runner)


# ── Synchronous within the timeout; otherwise return a pollable job. ──────────────
# The locked, process-local store replaces pending entries with terminal results.
# Short-lived single-user sessions make durability and eviction unnecessary here.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_DEFAULT_TIMEOUT = 60.0


def _new_job_id() -> str:
    """A collision-resistant, per-request job id (NOT derived at import — generated fresh)."""
    return secrets.token_hex(8)


def _default_timeout() -> float:
    """The sync-attempt budget in seconds: ``REBAR_PREVIEW_TIMEOUT`` if a positive number, else
    :data:`_DEFAULT_TIMEOUT` (60s)."""
    from rebar import config as _config

    return _config.resolve_preview_timeout(_DEFAULT_TIMEOUT)


def preview_or_job(
    request: dict[str, Any],
    *,
    repo_root: str | None,
    runner: Any = None,
    timeout: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """Return an inline preview or a pollable background job.

    Success and preview errors map to 200 and 400. Timeout leaves the worker running
    and returns ``(202, {status: "pending", job_id})`` for :func:`poll_job`.
    """
    from rebar._optional import OptionalDependencyError
    from rebar.llm.errors import LLMError

    if timeout is None:
        timeout = _default_timeout()
    job_id = _new_job_id()
    with _JOBS_LOCK:
        _JOBS[job_id] = {"status": "pending"}

    def _target() -> None:
        try:
            result = _run_preview_core(request, repo_root=repo_root, runner=runner)
            entry = {"status": "done", "result": result}
        except (PreviewError, LLMError, OptionalDependencyError) as exc:
            entry = {"status": "error", "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 — any worker crash becomes a polled error, never lost
            entry = {"status": "error", "error": f"preview failed: {exc}"}
        with _JOBS_LOCK:
            _JOBS[job_id] = entry

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        # Exceeded the sync budget → hand back the job id; the thread keeps running + will
        # store its result under job_id for the client's subsequent poll.
        return 202, {"status": "pending", "job_id": job_id}
    with _JOBS_LOCK:
        entry = _JOBS.pop(job_id, {"status": "error", "error": "job vanished"})
    if entry.get("status") == "error":
        return 400, {"error": str(entry.get("error"))}
    return 200, dict(entry["result"])


def poll_job(job_id: str) -> tuple[int, dict[str, Any]]:
    """Poll a background preview job. ``(200, {status:"pending"})`` while it runs;
    ``(200, {status:"done", result})`` (or ``{status:"done", error}``) once terminal, popping it;
    ``(404, {error})`` for an unknown/already-collected id."""
    if not job_id:
        return 400, {"error": "missing job_id"}
    with _JOBS_LOCK:
        entry = _JOBS.get(job_id)
        if entry is None:
            return 404, {"error": f"unknown job {job_id!r}"}
        if entry.get("status") == "pending":
            return 200, {"status": "pending"}
        _JOBS.pop(job_id, None)
    if entry.get("status") == "error":
        return 200, {"status": "done", "error": str(entry.get("error"))}
    return 200, {"status": "done", "result": entry["result"]}


def handle_preview_post(
    path: str, raw: bytes, *, repo_root: str | None
) -> tuple[int, dict[str, Any]]:
    """The single editor-handler entry for BOTH preview endpoints, returning ``(status, body)``
    (the editor maps nothing itself). ``…/status`` → :func:`poll_job` (by ``job_id``); otherwise
    the spike-gate :func:`preview_or_job`. A bad JSON body is a clean 400 ``{error}``."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return 400, {"error": f"bad JSON body: {exc}"}
    if not isinstance(data, dict):
        return 400, {"error": "body must be a JSON object"}
    if path.rstrip("/").endswith("/status"):
        return poll_job(str(data.get("job_id") or ""))
    return preview_or_job(data, repo_root=repo_root, runner=None)


# ── LLM path ─────────────────────────────────────────────────────────────────────
def _resolve_runner(runner: Any, repo_root: str | None) -> Any:
    """The injected runner, or one built from the gate config (needs the ``agents`` extra +
    credentials — a missing one raises ``LLMError``/``OptionalDependencyError`` that the editor
    handler maps to a 4xx, never a 500)."""
    if runner is not None:
        return runner
    from rebar.llm.config import resolve_gate_config
    from rebar.llm.runner import get_runner

    return get_runner(resolve_gate_config(repo_root))


def _preview_llm(
    criterion_id: str | None,
    inline: dict[str, Any] | None,
    fixture: dict[str, Any],
    repo_root: str | None,
    runner: Any,
) -> dict[str, Any]:
    """Fire ⇔ non-empty findings. An existing ``criterion_id`` runs through the landed
    ``eval_solver.run_case`` criterion arm; an unsaved ``inline`` criterion runs an ad-hoc
    descriptor directly through ``passes.pass1_chunk`` (no temp prompt file)."""
    resolved = _resolve_runner(runner, repo_root)
    plan = str(fixture.get("input") or "")

    if criterion_id:
        from rebar.llm.evals import eval_solver

        result = eval_solver.run_case(
            criterion_id, {"input": plan}, runner=resolved, repo_root=repo_root
        )
        findings = list(result.get("findings") or [])
    else:
        findings = _run_inline_finder(inline or {}, plan, repo_root, resolved)

    if findings:
        return {
            "verdict": "fire",
            "finding": findings[0],
            "rationale": f"criterion fired: {len(findings)} finding(s) surfaced over the fixture.",
        }
    return {
        "verdict": "no-fire",
        "finding": None,
        "rationale": "criterion did not fire (no findings over the fixture).",
    }


def _run_inline_finder(
    inline: dict[str, Any], plan: str, repo_root: str | None, runner: Any
) -> list[dict[str, Any]]:
    """Run an unsaved rubric through Pass-1 under its inline id, without a prompt file."""
    from rebar.llm.config import resolve_gate_config
    from rebar.llm.plan_review import passes

    routing = inline.get("routing") or {}
    exec_v = str(routing.get("exec", "1-TURN")).upper()
    desc = {
        "id": str(inline.get("id") or "preview"),
        "exec": exec_v,
        "scenario": str(inline.get("prompt") or ""),
        "facet": str(routing.get("facet", "misc")),
        "name": str(inline.get("id") or "preview"),
        "applies_at": {},
        "checklist": [],
        "block_threshold": routing.get("block_threshold", 0.95),
        "default_posture": routing.get("default_posture", "advisory"),
    }
    cfg = resolve_gate_config(repo_root)
    findings, _usage = passes.pass1_chunk(
        runner, cfg, plan=plan, chunk=[desc], agentic=exec_v == "AGENT"
    )
    return list(findings)


# ── DET path ─────────────────────────────────────────────────────────────────────
def _git_init(d: str) -> None:
    """Disposable git + fixture repo (mirrors ``eval_solver._git_init``) — some detectors
    (e.g. the gitleaks sentinel) require a git tree to scan."""
    subprocess.run(["git", "init", "-q", d], check=True)
    subprocess.run(["git", "-C", d, "config", "user.email", "preview@rebar.local"], check=True)
    subprocess.run(["git", "-C", d, "config", "user.name", "rebar-preview"], check=True)


def _infer_filename(reg_slice: Any, fixture: dict[str, Any]) -> str:
    """The fixture file name: ``fixture.filename`` if given, else a name the detector's
    ``languages``/``file_globs`` accept (``preview.<ext>``), else the generic ``preview.txt``."""
    name = fixture.get("filename")
    if isinstance(name, str) and name.strip():
        return name.strip()
    for det in reg_slice:
        for lang in det.languages:
            ext = _LANG_EXT.get(lang.lower())
            if ext:
                return f"preview.{ext}"
        for glob in det.file_globs:
            base = str(glob).rsplit("/", 1)[-1]
            if base and "*" not in base:
                return base
    return "preview.txt"


# raw-git-ok: disposable sandbox repo, not the tracker
def _preview_det(
    criterion_id: str | None,
    routing: dict[str, Any],
    fixture: dict[str, Any],
    repo_root: str | None,
) -> dict[str, Any]:
    """Materialize the fixture into a disposable repo, scan it with the criterion's detector
    slice, and map match→fire / abstain→per-``fail_mode`` / clean→no-fire."""
    from rebar.grounding import engine_b
    from rebar.llm.plan_review.det_invariants import _matching_detectors

    cid = criterion_id or "inline"
    selector = routing.get("detector")
    fail_mode = str(routing.get("fail_mode", "open")).lower()
    if not selector:
        return {
            "verdict": "no-fire",
            "finding": None,
            "rationale": "DET criterion has no 'detector' selector; nothing to scan.",
        }

    reg_slice = _matching_detectors(selector, repo_root)
    if reg_slice is None or not reg_slice.detectors:
        gap = "would block (fail_mode: closed)" if fail_mode == "closed" else "advisory only"
        return {
            "verdict": "no-fire",
            "finding": None,
            "rationale": f"no detector matched selector {selector!r} (coverage gap; {gap}).",
        }

    filename = _infer_filename(reg_slice, fixture)
    with tempfile.TemporaryDirectory(prefix="rebar-preview-") as tmp:
        _git_init(tmp)
        target = Path(tmp, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(fixture.get("input") or ""), encoding="utf-8")
        subprocess.run(["git", "-C", tmp, "add", "-A"], check=True)
        subprocess.run(["git", "-C", tmp, "commit", "-q", "-m", "fixture"], check=True)
        result = engine_b.scan(tmp, registry=reg_slice)

    matches = result.matches()
    if matches:
        return {
            "verdict": "fire",
            "finding": _det_finding(cid, matches[0], reg_slice),
            "rationale": f"detector matched the fixture ({len(matches)} match(es)).",
        }
    if result.abstains():
        gap = "would block (fail_mode: closed)" if fail_mode == "closed" else "advisory only"
        return {
            "verdict": "no-fire",
            "finding": None,
            "rationale": (
                f"detector abstained (tool unavailable / unsupported stack); coverage gap ({gap})."
            ),
        }
    return {
        "verdict": "no-fire",
        "finding": None,
        "rationale": "detector ran clean over the fixture (no match).",
    }


def _det_finding(cid: str, rec: dict[str, Any], reg_slice: Any) -> dict[str, Any]:
    """A findings.py-shaped finding built from a detector match record."""
    loc = rec.get("location") or {}
    file = loc.get("file") if isinstance(loc, dict) else None
    message = (
        rec.get("message")
        or rec.get("reason")
        or _first_rule_message(reg_slice)
        or (f"Detector {cid!r} matched the fixture.")
    )
    return {
        "finding": str(message),
        "criteria": [cid],
        "location": file or "",
        "evidence": [file] if file else [],
        "impact": "The fixture violates the project invariant this DET criterion enforces.",
        "suggested_fix": "Remediate the flagged pattern in the fixture.",
        "tier": "DET",
    }


def _first_rule_message(reg_slice: Any) -> str | None:
    for det in reg_slice:
        msg = (getattr(det, "rule", None) or {}).get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return None


# ── atomic authoring: routing overlay + activation ────────────────────────────────
def author_criterion(
    repo_root: str,
    criterion_id: str,
    meta: dict[str, Any],
    body: str,
    routing: dict[str, Any] | None,
) -> Path:
    """Write a dotted criterion's filesystem-safe rubric, then optionally activate it.

    Routing and activation share one atomic overlay update. Prompt-first failure leaves
    a harmless inactive rubric. Returns its path and propagates authoring errors.
    """
    from rebar.llm.criteria.ids import criterion_prompt_id
    from rebar.llm.prompting.prompt_library import CRITERION_CATEGORY, create_prompt
    from rebar.llm.prompting.prompts import write_front_matter

    text = write_front_matter({**meta, "category": CRITERION_CATEGORY}, body)
    path = create_prompt(criterion_prompt_id(criterion_id), text, repo_root=repo_root)
    if isinstance(routing, dict) and routing:
        author_criterion_overlay(repo_root, criterion_id, routing)
    return path


def write_criterion_overlay(repo_root: str, criterion_id: str, routing: dict[str, Any]) -> None:
    """Atomically update a criterion's routing and activation membership.

    Failure leaves the previously written rubric inactive. Callers must then invalidate
    the registry caches.
    """
    from rebar._store.fsutil import atomic_write

    path = Path(repo_root) / ".rebar" / _OVERLAY_FILENAME
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    # ``gate`` selects the overlay section and activation only; it is not persisted in
    # the entry. Missing or unknown values retain the plan_review default.
    gate = str(routing.get("gate", "plan_review"))
    if gate not in ("plan_review", "code_review"):
        gate = "plan_review"
    entry = {k: v for k, v in routing.items() if k != "gate"}
    section = data.get(gate)
    if not isinstance(section, dict):
        section = {}
    section[criterion_id] = entry
    data[gate] = section
    activate = data.get("activate")
    if isinstance(activate, dict):
        review_types = activate.get(criterion_id)
        if not isinstance(review_types, list):
            review_types = []
        if gate not in review_types:
            review_types.append(gate)
        activate[criterion_id] = review_types
    elif isinstance(activate, list):
        # Preserve the legacy list form when editing an existing overlay.
        if criterion_id not in activate:
            activate.append(criterion_id)
    else:
        activate = {criterion_id: [gate]}
    data["activate"] = activate
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n", permissions=0o600)


def author_criterion_overlay(repo_root: str, criterion_id: str, routing: dict[str, Any]) -> None:
    """Activate an explicit dotted criterion id and refresh the registry caches.

    The logical id is never recovered from its one-way filename mapping. Validation
    re-resolves the merged registry; failure restores the prior overlay and leaves the
    prompt inactive.
    """
    from rebar.llm.plan_review import registry
    from rebar.llm.prompting.prompt_library import _invalidate_caches

    cid = criterion_id
    path = Path(repo_root) / ".rebar" / _OVERLAY_FILENAME
    prior = path.read_text(encoding="utf-8") if path.is_file() else None
    write_criterion_overlay(repo_root, cid, routing)
    _invalidate_caches()
    try:
        registry.effective_routing(repo_root)
        registry.effective_criteria(repo_root)
    except Exception:
        # Roll the overlay back to its prior state (or remove a file we created) so a rejected
        # authoring attempt can't leave a load-breaking overlay behind.
        if prior is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(prior, encoding="utf-8")
        _invalidate_caches()
        raise


_OVERLAY_FILENAME = "criteria_routing.json"
