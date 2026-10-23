"""Stdlib Gerrit REST and git client for review-bot operations.
Authenticated /a/ calls use basic auth and strip Gerrit's XSSI prefix.
The client reads patches, revision files, votes, messages, and events,
posts reviews or comments, tags and submits changes, and fetches a
review checkout. The async voter offloads its blocking calls."""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from rebar._snapshot.git_fetch import stall_abort_args
from rebar.review_bot.config import ReceiverConfig

logger = logging.getLogger("rebar.review_bot.gerrit_client")

_XSSI = ")]}'"
_LABEL = "LLM-Review"
_ROBOT_ID = "rebar-review-bot"

#: Bound every git call to 300 seconds for cold clones. Network fetches
#: also abort when throughput stalls, without interrupting slow progress.
_GIT_TIMEOUT = 300


def _strip_xssi(text: str) -> str:
    """Strip Gerrit's ``)]}'`` XSSI prefix (and trailing newline) before JSON parse."""
    text = text.lstrip()
    if text.startswith(_XSSI):
        text = text[len(_XSSI) :]
    return text.strip()


def _git_op(cmd: Any) -> str:
    """Return the git subcommand from list or string timeout arguments.
    Skip global -C options and fall back to git."""
    parts = list(cmd) if isinstance(cmd, (list, tuple)) else str(cmd).split()
    i = 1 if parts and str(parts[0]).endswith("git") else 0
    while i < len(parts):
        token = str(parts[i])
        if token == "-C":  # global option taking a value
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        return token
    return "git"


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
    def _request(
        self, method: str, path: str, *, body: dict | None = None, timeout: float = 60
    ) -> tuple[int, str]:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": self._auth, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            raise GerritError(
                f"{method} {path} -> HTTP {exc.code}: {detail[:500]}", status=exc.code
            ) from exc
        except urllib.error.URLError as exc:
            raise GerritError(f"{method} {path} transport error: {exc.reason}") from exc

    def check_auth(self) -> None:
        """Raise if the configured bot credential cannot authenticate to Gerrit."""
        self._request("GET", "/a/accounts/self", timeout=5)

    def _get_json(self, path: str) -> Any:
        status, text = self._request("GET", path)
        try:
            return json.loads(_strip_xssi(text))
        except json.JSONDecodeError as exc:
            raise GerritError(f"GET {path}: unparseable JSON body", status=status) from exc

    # ── operations ────────────────────────────────────────────────────────────
    def get_patch(self, change_id: str, revision: str = "current") -> str:
        """Return format-patch text from XSSI-prefixed JSON, decoding base64 if present.
        Also accept a base64 text/plain body."""
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

    # Review merge conflict resolutions through the auto-merge file delta.
    # Bare /patch returns 409 and first-parent diff includes the full feature.
    # Synthetic paths are not conflict files.
    MAGIC_PATHS = frozenset({"/COMMIT_MSG", "/MERGE_LIST"})

    def get_commit(self, change_id: str, revision: str = "current") -> dict:
        """Return the revision's ``CommitInfo`` (used for merge detection: a merge revision
        has ``len(parents) >= 2``). One extra REST GET per review; a failure here fails
        closed in the voter."""
        return self._get_json(f"/a/changes/{self._q(change_id)}/revisions/{revision}/commit")

    def get_merge_files(self, change_id: str, revision: str = "current") -> dict[str, dict]:
        """Return a merge revision's delta against Gerrit's auto-merge base.
        A clean merge contains only pseudo-paths. Bare /patch returns 409
        for merge revisions."""
        return self.get_revision_files(change_id, revision)

    def get_revision_files(self, change_id: str, revision: str = "current") -> dict[str, dict]:
        """Return the revision file map used to validate inline finding paths.
        For merges, the same endpoint without base or parent yields the
        auto-merge delta."""
        d = self._get_json(f"/a/changes/{self._q(change_id)}/revisions/{revision}/files")
        return d if isinstance(d, dict) else {}

    def get_file_diff(self, change_id: str, file_path: str, revision: str = "current") -> dict:
        """Per-file ``DiffInfo`` against the auto-merge base (no ``base``/``parent`` param) —
        the conflict-resolution hunks for ``file_path`` in a merge revision."""
        fp = urllib.parse.quote(file_path, safe="")
        return self._get_json(
            f"/a/changes/{self._q(change_id)}/revisions/{revision}/files/{fp}/diff"
        )

    def get_mergelist(self, change_id: str, revision: str = "current") -> list[dict]:
        """The commits a merge revision integrates (``GET .../mergelist`` — the first-parent
        delta list). Returns ``[]`` for a non-merge revision or an unexpected body."""
        d = self._get_json(f"/a/changes/{self._q(change_id)}/revisions/{revision}/mergelist")
        return d if isinstance(d, list) else []

    def has_llm_review_vote(self, change_id: str, revision: str = "current") -> bool:
        """Return whether any account has a nonzero LLM-Review vote on this
        revision. Gerrit-side state complements local deduplication."""
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

    def reset_llm_review_vote(self, change_id: str, revision: str) -> int:
        """Reset this bot's vote to neutral on the exact revision.
        The fixed message and value create a durable rerun event without
        accepting contributor-controlled vote text."""
        body = {
            "tag": "autogenerated:rebar-rerun",
            "notify": "NONE",
            "labels": {_LABEL: 0},
            "message": "rebar: fresh fail-closed LLM review requested; vote reset to neutral.",
        }
        status, _ = self._request(
            "POST",
            f"/a/changes/{self._q(change_id)}/revisions/{revision}/review",
            body=body,
        )
        if not (200 <= status < 300):
            raise GerritError(f"reset_llm_review_vote({change_id}) -> HTTP {status}", status=status)
        return status

    def get_change_event(self, change_id: str) -> dict | None:
        """Build a webhook-shaped manual-rerun event for the current revision.
        Return None when Gerrit reports no current revision."""
        d = self._get_json(f"/a/changes/{self._q(change_id)}?o=CURRENT_REVISION")
        cur = (d or {}).get("current_revision")
        if not cur:
            return None
        rev = ((d.get("revisions") or {}).get(cur)) or {}
        return {
            "type": "manual-rerun",
            "change": {
                # ChangeInfo.id is a REST lookup id (often project-qualified);
                # change_id is Gerrit's canonical, stable Change-Id.
                "id": d.get("change_id") or d.get("id") or change_id,
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
        comments: dict | None = None,
    ) -> int:
        """Post LLM-Review with the autogenerated tag, no notification, and a
        patchset-level robot comment. Add ordinary inline comments only when
        provided. Raise GerritError on non-2xx so failed publication writes no
        dedup row."""
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
        if comments:
            body["comments"] = comments
        status, _ = self._request(
            "POST",
            f"/a/changes/{self._q(change_id)}/revisions/{revision}/review",
            body=body,
        )
        if not (200 <= status < 300):
            raise GerritError(f"post_vote({change_id}) -> HTTP {status}", status=status)
        return status

    def get_change_messages(self, change_id: str) -> list[dict]:
        """Return change messages in Gerrit order. Scan their lines for verdict
        tags because Gerrit prepends a Patch Set header."""
        d = self._get_json(f"/a/changes/{self._q(change_id)}/messages")
        return d if isinstance(d, list) else []

    def post_comment(self, change_id: str, revision: str, message: str) -> int:
        """Post a change message with no labels member, so a retrigger reply
        cannot alter or copy vote state."""
        body = {"message": message}
        status, _ = self._request(
            "POST",
            f"/a/changes/{self._q(change_id)}/revisions/{revision}/review",
            body=body,
        )
        if not (200 <= status < 300):
            raise GerritError(f"post_comment({change_id}) -> HTTP {status}", status=status)
        return status

    def add_session_hashtag(self, change_id: str, hashtag: str) -> int:
        """Stamp ``change_id`` with a Gerrit-native session hashtag.

        Under the shared bot account, Gerrit ``owner:self`` cannot distinguish concurrent
        local sessions. Hashtags are UI-visible Gerrit metadata, so they make the driving
        session answerable without introducing a local serialization point.
        """
        status, _ = self._request(
            "POST",
            f"/a/changes/{self._q(change_id)}/hashtags",
            body={"add": [hashtag]},
        )
        if not (200 <= status < 300):
            raise GerritError(f"add_session_hashtag({change_id}) -> HTTP {status}", status=status)
        return status

    def submit_revision(self, change_id: str, revision: str) -> int:
        """Submit exactly ``revision`` of ``change_id``.

        Gerrit's revision-scoped submit endpoint is a compare-and-submit guard: if another
        session uploaded a newer patch set after votes were inspected, Gerrit returns HTTP
        409 ("revision ... is not current revision") instead of submitting stale state.
        """
        status, _ = self._request(
            "POST",
            f"/a/changes/{self._q(change_id)}/revisions/{revision}/submit",
            body={},
        )
        if not (200 <= status < 300):
            raise GerritError(
                f"submit_revision({change_id}, {revision}) -> HTTP {status}", status=status
            )
        return status

    def list_events(self, since: str | None = None) -> list[dict]:
        """Read newline-delimited events from the slash-terminated events-log
        endpoint. Optional since maps to Gerrit's inclusive t1 window."""
        path = "/a/plugins/events-log/events/"
        if since:
            path += "?t1=" + urllib.parse.quote(str(since), safe="")
        _status, text = self._request("GET", path)
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

    # raw-git-ok: disposable sandbox repo, not the tracker
    def clone_change_ref(self, change_number: int, revision_ref: str, dest: str) -> str:
        """Fetch the patchset ref into a disposable checkout and return its path.
        Use HTTPS bot credentials, bounded git calls, and a stall abort on
        network fetches. Fetch the tickets branch from its mirror for attested
        review. Redact credentials from failures."""
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
            subprocess.run(
                ["git", "init", "-q", dest],
                check=True,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            subprocess.run(
                # The -c pairs arm the throughput-keyed stall abort and MUST precede the
                # subcommand; see stall_abort_args().
                [
                    "git",
                    "-C",
                    dest,
                    *stall_abort_args(),
                    "fetch",
                    "-q",
                    "--depth",
                    "2",
                    repo_url,
                    revision_ref,
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            subprocess.run(
                ["git", "-C", dest, "checkout", "-q", "FETCH_HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            # Fetch the orphan tickets branch from the GitHub mirror as origin/tickets.
            # The attested gate needs its current tree. Gerrit hosts only code refs.
            # Depth 1 is enough for materialization.
            subprocess.run(
                ["git", "-C", dest, "remote", "add", "origin", self._cfg.tickets_remote],
                check=True,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    dest,
                    *stall_abort_args(),
                    "fetch",
                    "-q",
                    "--depth",
                    "1",
                    "origin",
                    "refs/heads/tickets:refs/remotes/origin/tickets",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            # Convert git timeouts through the same credential redaction as other
            # fetch failures. Timeout arguments can contain the tokenized URL.
            raise GerritError(
                f"clone_change_ref(change={change_number}, ref={revision_ref}) failed: "
                f"git {_git_op(exc.cmd)} timed out after {_GIT_TIMEOUT} seconds: "
                f"{self._redact(exc.cmd)[:500]}"
            ) from exc
        except (OSError, subprocess.CalledProcessError) as exc:
            stderr = getattr(exc, "stderr", "") or ""
            # Redact both raw and percent-encoded tokens from git failures.
            redacted = self._redact(stderr)
            raise GerritError(
                f"clone_change_ref(change={change_number}, ref={revision_ref}) failed: "
                f"{redacted[:500]}"
            ) from exc
        return dest

    def _redact(self, text: Any) -> str:
        """Remove raw and URL-encoded bot tokens from git error text."""
        tok = self._cfg.gerrit_bot_token
        out = str(text)
        if not tok:
            return out
        return out.replace(tok, "***").replace(urllib.parse.quote(tok, safe=""), "***")

    @staticmethod
    def _q(change_id: str) -> str:
        """URL-encode a change id (``project~branch~Iabc`` ids contain ``~``)."""
        return urllib.parse.quote(str(change_id), safe="")
