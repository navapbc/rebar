"""Live must-fire / must-not-fire preview for a plan-review criterion (story 6e31).

The visual editor authors criteria; this module lets an author PROBE one against a
fixture and see whether it fires — the same runners the real gate uses, but over an
isolated fixture instead of a ticket:

* an **LLM criterion** (``exec: 1-TURN`` / ``AGENT``) runs as its Pass-1 finder over the
  fixture text (``eval_solver.run_case`` criterion arm, story 55b8, for an existing id;
  an ad-hoc ``passes.pass1_chunk`` for an unsaved ``inline`` prompt). Fire ⇔ non-empty
  findings.
* a **DET invariant** (``exec: DET``) resolves its ``detector`` selector, materializes the
  fixture into a disposable temp repo, and runs the grounding scan the way
  :func:`rebar.llm.plan_review.det_invariants._run_one` does (story 7f0d). A ``match`` ⇒
  fire; an ``abstain`` ⇒ no-fire, reported per the criterion's ``fail_mode``.

Container/ISF finders (G3/G4/ISF) need a ticket graph or a session log, so they are NOT
previewable inline — :class:`PreviewError` (mapped to a 4xx by the editor handler).

The preview is SYNCHRONOUS with a configurable timeout (default 60s, overridable via
``REBAR_PREVIEW_TIMEOUT``). The **spike-gate** threshold + fallback (:func:`preview_or_job`):
the endpoint attempts the preview within the timeout; if it finishes in time it returns the
verdict inline (HTTP 200); if it EXCEEDS the timeout it registers the still-running preview as
a background JOB and returns ``{status: "pending", job_id}`` (HTTP 202), and the client polls
``POST /criterion/preview/status`` (:func:`poll_job`) until it reads
``{status: "done", result: {verdict, …}}``. The job store is an in-memory dict keyed by a
per-request ``secrets``-derived id, guarded by a lock — so the editor's single-threaded
``ThreadingHTTPServer`` handler never blocks past the timeout. (The direct
:func:`preview_criterion` keeps a simple sync-with-timeout contract for library/eval callers.)

Also hosts :func:`write_criterion_overlay` — the single ATOMIC overlay write that couples
a project criterion's routing entry with its ``activate`` membership so authoring can never
leave a half-active criterion (the rubric prompt is written first, harmlessly, by
``create_prompt``).
"""

from __future__ import annotations

import json
import os
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
    "preview_criterion_response",
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
    """Run a criterion against a fixture and return ``{verdict, finding?, rationale, timed_out?}``.

    ``request`` = ``{criterion_id?, inline?: {prompt?, routing}, fixture: {input, filename?}}``.
    Supply EITHER an existing ``criterion_id`` (its routing/descriptor is resolved from the
    effective registry) OR an ``inline`` criterion (a routing dict + optional prompt text, run
    ad-hoc — for previewing an unsaved authoring draft). The criterion's ``exec`` is read
    DIRECTLY from the routing (never ``exec_tier``, which has no DET arm): ``DET`` → a grounding
    scan of the materialized fixture; otherwise → the LLM Pass-1 finder path.

    Raises :class:`PreviewError` for an unknown criterion or a container/ISF finder. On timeout,
    returns a no-fire verdict with ``timed_out: True`` (never blocks forever)."""
    # Run the (LLM or DET) call under a timeout so a slow model / scan never wedges the
    # editor's single-threaded HTTP handler. The worker thread is left to finish in the
    # background on timeout (the async job+poll fallback via preview_or_job continues it).
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


# ── spike-gate: sync-within-timeout, else a background job + poll ─────────────────
# An in-memory job store keyed by a per-request secrets-derived id, guarded by a lock. A
# job is registered "pending" at submit and OVERWRITTEN with its terminal state by the
# worker thread. Deliberately process-local + unbounded-until-polled (an editor session is
# short-lived + single-user); a durable/distributed store is out of scope for the MVP.
_JOBS: dict[str, dict[str, Any]] = {}
_JOBS_LOCK = threading.Lock()
_DEFAULT_TIMEOUT = 60.0


def _new_job_id() -> str:
    """A collision-resistant, per-request job id (NOT derived at import — generated fresh)."""
    return secrets.token_hex(8)


def _default_timeout() -> float:
    """The sync-attempt budget in seconds: ``REBAR_PREVIEW_TIMEOUT`` if a positive number, else
    :data:`_DEFAULT_TIMEOUT` (60s)."""
    raw = os.environ.get("REBAR_PREVIEW_TIMEOUT")
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return _DEFAULT_TIMEOUT


def preview_or_job(
    request: dict[str, Any],
    *,
    repo_root: str | None,
    runner: Any = None,
    timeout: float | None = None,
) -> tuple[int, dict[str, Any]]:
    """The spike-gate endpoint shim. Run the preview on a background thread and wait up to
    ``timeout`` (default :func:`_default_timeout`): if it finishes in time, return ``(200,
    verdict)`` (or ``(400, {error})`` for a :class:`PreviewError` / missing agents extra); if it
    EXCEEDS the timeout, leave it running and return ``(202, {status:"pending", job_id})`` — the
    client polls :func:`poll_job`. Never blocks past ``timeout``."""
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


def preview_criterion_response(raw: bytes, *, repo_root: str | None) -> tuple[int, dict[str, Any]]:
    """Back-compat SYNC shim (library/tests): parse the raw POST body, run
    :func:`preview_criterion`, and return ``(status_code, body)``. A bad body / a
    :class:`PreviewError` / a missing ``agents`` extra or credentials (``LLMError`` /
    ``OptionalDependencyError``) is a clean 400 ``{error}``. The live editor endpoint uses
    :func:`handle_preview_post` (async-aware)."""
    from rebar._optional import OptionalDependencyError
    from rebar.llm.errors import LLMError

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        return 400, {"error": f"bad JSON body: {exc}"}
    try:
        return 200, preview_criterion(data, repo_root=repo_root, runner=None)
    except (PreviewError, LLMError, OptionalDependencyError) as exc:
        return 400, {"error": str(exc)}


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
        from rebar.llm import eval_solver

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
    """Run an UNSAVED inline criterion as a Pass-1 finder via an ad-hoc descriptor — no temp
    prompt file. The descriptor's ``id`` is the tag the finder must attribute its findings to
    (``inline.id`` or ``preview``); ``inline.prompt`` is the rubric body."""
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
    return list(passes.pass1_chunk(runner, cfg, plan=plan, chunk=[desc], agentic=exec_v == "AGENT"))


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
    """Author a criterion END-TO-END from its LOGICAL (dotted-for-project) id: write its rubric at
    the filesystem-safe ``criterion_prompt_id(criterion_id)`` (task stew-kid-motif — so a net-new
    ``project.<name>`` is authorable despite the ``.``-free filename rule), then, if ``routing`` is
    given, atomically write + activate its overlay entry keyed by the dotted id. Prompt-first: a
    failed overlay leaves an inactive, harmless rubric. Returns the rubric ``Path``. Raises
    ``LibraryWriteError``/``PromptError`` (bad id/rubric) or ``RegistryError`` (bad overlay) — the
    HTTP caller maps either to a 4xx."""
    from rebar.llm.criteria.ids import criterion_prompt_id
    from rebar.llm.prompt_library import CRITERION_CATEGORY, create_prompt
    from rebar.llm.prompts import write_front_matter

    text = write_front_matter({**meta, "category": CRITERION_CATEGORY}, body)
    path = create_prompt(criterion_prompt_id(criterion_id), text, repo_root=repo_root)
    if isinstance(routing, dict) and routing:
        author_criterion_overlay(repo_root, criterion_id, routing)
    return path


def write_criterion_overlay(repo_root: str, criterion_id: str, routing: dict[str, Any]) -> None:
    """Write (read-modify-write) the project criterion's routing entry AND its ``activate``
    membership into ``.rebar/criteria_routing.json`` in a SINGLE atomic replace.

    Activation requires BOTH the routing entry and the ``activate`` id (ef7e semantics), and
    both are set in the same atomic write — so a project criterion is never left half-active. A
    failed overlay write leaves the (already-written) rubric prompt harmlessly inactive. Callers
    should invalidate the registry caches (``prompt_library._invalidate_caches``) after writing."""
    from .prompt_authoring import _atomic_write

    path = Path(repo_root) / ".rebar" / _OVERLAY_FILENAME
    data: dict[str, Any] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, ValueError):
            data = {}
    plan_review = data.get("plan_review")
    if not isinstance(plan_review, dict):
        plan_review = {}
    plan_review[criterion_id] = routing
    data["plan_review"] = plan_review
    activate = data.get("activate")
    if not isinstance(activate, list):
        activate = []
    if criterion_id not in activate:
        activate.append(criterion_id)
    data["activate"] = activate
    _atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def author_criterion_overlay(repo_root: str, criterion_id: str, routing: dict[str, Any]) -> None:
    """Author a criterion's routing overlay from its LOGICAL (dotted) criterion id, atomically
    write its routing + activation (:func:`write_criterion_overlay`), then invalidate the
    registry caches so it is immediately active. Called AFTER the rubric prompt is written
    (prompt-first).

    ``criterion_id`` is the DOTTED logical id (``project.<name>`` for a project criterion, or a
    built-in id for a re-tune) — it is passed EXPLICITLY, never reverse-derived from the sanitized
    rubric prompt id (which is a one-way, non-reversible map — task stew-kid-motif).

    VALIDATE-then-rollback: after writing, the merged overlay is re-resolved
    (``effective_routing`` + ``effective_criteria``). If it is invalid (e.g. a net-new id that
    is not ``project.<name>``-prefixed, or a name outside the filesystem-safe charset), the prior
    overlay is restored and the :class:`RegistryError` re-raised, so a bad authoring attempt NEVER
    persists a broken overlay (the caller maps it to a 4xx; the just-written prompt is left
    harmlessly inactive)."""
    from rebar.llm.plan_review import registry
    from rebar.llm.prompt_library import _invalidate_caches

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
