"""A small Gerrit REST + git client for the review-bot (epic d251 / S4b).

STDLIB ONLY (``urllib``/``subprocess``) — deliberately NO new ``httpx`` dependency.
The receiver makes only a handful of authenticated calls per patchset, so the
synchronous stdlib HTTP client is sufficient; the async voter runs these blocking
calls off the event loop via ``asyncio.to_thread`` (see ``voter.py``). The
``reviewbot`` extra therefore stays ``fastapi`` + ``uvicorn`` only.

All authenticated REST calls hit the ``/a/`` (authenticated) namespace with HTTP
basic auth (``BOT_USER:GERRIT_BOT_TOKEN``). Gerrit prefixes every JSON response with
the XSSI guard ``)]}'`` which we strip before parsing.

Endpoints used:
- ``GET  /a/changes/{id}/revisions/{rev}/patch`` — base64 git format-patch → diff text.
- ``GET  /a/changes/{id}/revisions/{rev}/review`` — current votes (existing LLM-Review?).
- ``POST /a/changes/{id}/revisions/{rev}/review`` — cast the LLM-Review label + robot comment.
- ``GET  /a/plugins/events-log/events/`` — backfill source (reconciler).
plus a git clone/fetch of the change ref into a working tree for the reviewer.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from rebar.review_bot.config import ReceiverConfig

logger = logging.getLogger("rebar.review_bot.gerrit_client")

_XSSI = ")]}'"
_LABEL = "LLM-Review"
_ROBOT_ID = "rebar-review-bot"


def _strip_xssi(text: str) -> str:
    """Strip Gerrit's ``)]}'`` XSSI prefix (and trailing newline) before JSON parse."""
    text = text.lstrip()
    if text.startswith(_XSSI):
        text = text[len(_XSSI) :]
    return text.strip()


class GerritError(RuntimeError):
    """A Gerrit REST call failed (non-2xx, transport error, or unusable body).

    Carries the HTTP status (``None`` for a transport-level failure) so the voter can
    log it in the structured ``VOTER_ERROR`` line and fail closed."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class GerritClient:
    """Authenticated Gerrit REST + git operations for one bot identity."""

    def __init__(self, config: ReceiverConfig):
        self._cfg = config
        self._base = config.gerrit_base_url.rstrip("/")
        # Basic-auth header for the /a/ namespace (bot user + HTTP token).
        raw = f"{config.bot_user}:{config.gerrit_bot_token}".encode()
        self._auth = "Basic " + base64.b64encode(raw).decode("ascii")

    # ── low-level HTTP ────────────────────────────────────────────────────────
    def _request(self, method: str, path: str, *, body: dict | None = None) -> tuple[int, str]:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — fixed base URL
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise GerritError(
                f"{method} {path} -> HTTP {exc.code}: {detail[:500]}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise GerritError(f"{method} {path} transport error: {exc.reason}") from exc

    def _get_json(self, path: str) -> Any:
        status, text = self._request("GET", path)
        try:
            return json.loads(_strip_xssi(text))
        except json.JSONDecodeError as exc:
            raise GerritError(f"GET {path}: unparseable JSON body", status=status) from exc

    # ── operations ────────────────────────────────────────────────────────────
    def get_patch(self, change_id: str, revision: str = "current") -> str:
        """Return the change's diff (the unified git ``format-patch`` text).

        The ``/patch`` endpoint's body shape depends on the request ``Accept``:
        with ``Accept: application/json`` (what this client sends), Gerrit returns
        the patch as an XSSI-guarded JSON *string* of the raw format-patch text
        (NOT base64 — that is only the default text/plain form). So we strip the
        XSSI prefix and JSON-decode to the diff text directly."""
        status, text = self._request(
            "GET", f"/a/changes/{self._q(change_id)}/revisions/{revision}/patch"
        )
        stripped = _strip_xssi(text)
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            # Defensive fallback: a text/plain base64 body (if Accept is ever changed).
            try:
                return base64.b64decode(stripped).decode("utf-8", "replace")
            except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
                raise GerritError(
                    f"get_patch({change_id}): body was neither JSON nor base64", status=status
                ) from exc
        # Gerrit may return the raw patch text directly, or base64 inside the JSON
        # string (older behaviour). Detect a base64 body and decode if needed.
        if isinstance(decoded, str) and decoded.lstrip().startswith(("From ", "diff --git")):
            return decoded
        try:
            return base64.b64decode(decoded).decode("utf-8", "replace")
        except (ValueError, base64.binascii.Error):  # type: ignore[attr-defined]
            return decoded if isinstance(decoded, str) else str(decoded)

    def has_llm_review_vote(self, change_id: str, revision: str = "current") -> bool:
        """True if the given revision already carries a NON-ZERO ``LLM-Review`` vote.

        Reads ``/revisions/{rev}/review`` and inspects the ``labels.LLM-Review.all``
        per-account votes — any non-zero value means the patchset has already been
        voted (by the bot or an admin), so we skip. Authoritative Gerrit-side guard
        that complements the local dedup store."""
        detail = self._get_json(f"/a/changes/{self._q(change_id)}/revisions/{revision}/review")
        labels = (detail or {}).get("labels") or {}
        entry = labels.get(_LABEL) or {}
        for vote in entry.get("all") or []:
            try:
                if int(vote.get("value") or 0) != 0:
                    return True
            except (TypeError, ValueError):
                continue
        return False

    def get_change_event(self, change_id: str) -> dict | None:
        """Build a webhook-shaped event for ``change_id`` from its CURRENT revision.

        Used by the manual ``/rerun`` path (which has only a change id, not a live
        webhook payload). Queries ``/a/changes/{id}?o=CURRENT_REVISION`` and shapes the
        result like a ``patchset-created`` event so it flows through the SAME
        ``voter.review_and_vote`` path. Returns ``None`` if the change has no current
        revision."""
        d = self._get_json(f"/a/changes/{self._q(change_id)}?o=CURRENT_REVISION")
        cur = (d or {}).get("current_revision")
        if not cur:
            return None
        rev = ((d.get("revisions") or {}).get(cur)) or {}
        return {
            "type": "manual-rerun",
            "change": {
                "id": d.get("id") or change_id,
                "number": d.get("_number"),
                "project": d.get("project"),
            },
            "patchSet": {
                "number": rev.get("_number"),
                "revision": cur,
                "ref": rev.get("ref"),
            },
        }

    def post_vote(
        self,
        change_id: str,
        revision: str,
        value: int,
        message: str,
        robot_comments: dict | None = None,
    ) -> int:
        """Cast the ``LLM-Review`` label on ``revision`` and return the HTTP status.

        Posts ``tag=autogenerated:rebar``, ``notify=NONE``, ``labels={LLM-Review: value}``
        and (default) a single patchset-level robot comment under the magic path
        ``/PATCHSET_LEVEL`` — a robot comment with NO path is a 400, so we always anchor
        it there. Raises ``GerritError`` on a non-2xx response (the voter treats that as
        a fail-closed BLOCK; no dedup row is written)."""
        if robot_comments is None:
            robot_comments = {
                "/PATCHSET_LEVEL": [
                    {
                        "robot_id": _ROBOT_ID,
                        # Deterministic run id (change-rev) so a webhook + a backfill for
                        # the same patchset reference the same robot run, not two.
                        "robot_run_id": f"{change_id}-{revision}",
                        "message": message or "rebar code review.",
                    }
                ]
            }
        body = {
            "tag": "autogenerated:rebar",
            "notify": "NONE",
            "labels": {_LABEL: value},
            "message": message or "rebar code review.",
            "robot_comments": robot_comments,
        }
        status, _ = self._request(
            "POST",
            f"/a/changes/{self._q(change_id)}/revisions/{revision}/review",
            body=body,
        )
        if not (200 <= status < 300):
            raise GerritError(f"post_vote({change_id}) -> HTTP {status}", status=status)
        return status

    def list_events(self, since: str | None = None) -> list[dict]:
        """Fetch the events-log plugin's events (reconciler source).

        The endpoint is ``/a/plugins/events-log/events/`` — the TRAILING SLASH is
        required (without it Gerrit 404s). The body is newline-delimited JSON events
        (one per line), NOT a JSON array, so parse line-by-line.

        ``since`` (optional) restricts the window to events at/after a ``t1`` time
        (the events-log REST ``?t1=`` query param, ``yyyy-MM-dd HH:mm:ss`` UTC). The
        reconciler passes its persisted cursor so each poll fetches only the new tail
        rather than rescanning the whole log. A blank/None ``since`` fetches all
        retained events (first run / no cursor)."""
        path = "/a/plugins/events-log/events/"
        if since:
            path += "?t1=" + urllib.parse.quote(str(since), safe="")
        status, text = self._request("GET", path)
        events: list[dict] = []
        for line in _strip_xssi(text).splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.debug("events-log: skipping unparseable line")
        return events

    def clone_change_ref(self, change_number: int, revision_ref: str, dest: str) -> str:
        """Clone the project at the change's ref into ``dest`` (a working tree for the
        reviewer) and return ``dest``.

        Fetches over HTTPS using the bot token in the URL (``user:token@host``). The
        ``revision_ref`` is the patch set's ref, e.g. ``refs/changes/NN/CHANGE/REV``,
        carried in the webhook ``patchSet.ref``. Raises ``GerritError`` on failure."""
        parsed = urllib.parse.urlsplit(self._base)
        userinfo = (
            f"{urllib.parse.quote(self._cfg.bot_user, safe='')}:"
            f"{urllib.parse.quote(self._cfg.gerrit_bot_token, safe='')}@"
        )
        netloc = f"{userinfo}{parsed.netloc}"
        repo_url = urllib.parse.urlunsplit(
            (parsed.scheme or "http", netloc, f"/a/{self._cfg.project}", "", "")
        )
        try:
            subprocess.run(["git", "init", "-q", dest], check=True, capture_output=True, text=True)
            subprocess.run(
                ["git", "-C", dest, "fetch", "-q", "--depth", "2", repo_url, revision_ref],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "-C", dest, "checkout", "-q", "FETCH_HEAD"],
                check=True,
                capture_output=True,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            # Never let the token leak into a log line via the URL — redact BOTH the
            # raw token AND its percent-encoded form (the token is URL-quoted into the
            # fetch URL, so git's error output would echo the encoded form).
            tok = self._cfg.gerrit_bot_token
            redacted = (
                str(stderr).replace(tok, "***").replace(urllib.parse.quote(tok, safe=""), "***")
            )
            raise GerritError(
                f"clone_change_ref(change={change_number}, ref={revision_ref}) failed: "
                f"{redacted[:500]}"
            ) from exc
        return dest

    @staticmethod
    def _q(change_id: str) -> str:
        """URL-encode a change id (``project~branch~Iabc`` ids contain ``~``)."""
        return urllib.parse.quote(str(change_id), safe="")
