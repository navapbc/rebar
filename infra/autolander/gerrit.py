"""Thin, STDLIB-ONLY Gerrit REST helper for the serial auto-lander (epic f1fa / S2a).

Mirrors the `urllib` + Basic-auth + XSSI pattern of `src/rebar/review_bot/gerrit_client.py`
but is a standalone module: NO new dependency (no pygerrit2), NO `import rebar`. Covers only
the calls the lander loop needs: query changes, get a change, RelatedChanges, rebase,
rebase:chain, submit — all with the `o=DETAILED_LABELS` option where labels/approval dates
are read.

The HTTP boundary is injectable via `transport` so the loop + this client are unit-testable
without a live Gerrit: a transport is `Callable[[method, path, body|None], tuple[int, str]]`
returning `(status_code, response_text)` where response_text MAY carry Gerrit's XSSI prefix.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

_XSSI = ")]}'"

Transport = Callable[[str, str, "dict | None"], "tuple[int, str]"]


class GerritError(RuntimeError):
    """A non-2xx Gerrit response or a transport failure."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"gerrit {status}: {message}")
        self.status = status


def _strip_xssi(text: str) -> str:
    """Return `text` with Gerrit's magic XSSI prefix line removed, if present."""
    text = text.lstrip()
    if text.startswith(_XSSI):
        text = text[len(_XSSI) :]
    return text.strip()


def _default_transport(base_url: str, auth: str) -> Transport:
    """Build a urllib-based transport over the authenticated `/a/` API.

    Not exercised by tests; mirrors the review-bot's urllib + Basic-auth pattern.
    """

    def transport(method: str, path: str, body: dict | None) -> tuple[int, str]:
        url = f"{base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": auth, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310 — fixed base URL
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace") if exc.fp else ""
            return exc.code, detail
        except urllib.error.URLError as exc:
            raise GerritError(0, f"{method} {path} transport error: {exc.reason}") from exc

    return transport


class GerritClient:
    """Minimal Gerrit REST client over the authenticated `/a/` API."""

    def __init__(
        self,
        base_url: str,
        user: str,
        token: str,
        *,
        transport: Transport | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        raw = f"{user}:{token}".encode()
        self._auth = "Basic " + base64.b64encode(raw).decode("ascii")
        self._transport: Transport = transport or _default_transport(self._base, self._auth)

    # --- low level --------------------------------------------------------
    def _request(self, method: str, path: str, *, body: dict | None = None) -> tuple[int, str]:
        """Perform one request via the transport; raise GerritError on non-2xx."""
        status, text = self._transport(method, path, body)
        if status >= 300:
            raise GerritError(status, _strip_xssi(text))
        return status, text

    def _get_json(self, path: str) -> Any:
        """GET `path`, strip XSSI, JSON-decode."""
        _status, text = self._request("GET", path)
        return json.loads(_strip_xssi(text))

    # --- API the lander needs --------------------------------------------
    def query_changes(self, query: str, opts: list[str] | None = None) -> list[dict]:
        """`GET /changes/?q=<query>&o=<opt>...` -> list of ChangeInfo dicts."""
        path = "/changes/?q=" + urllib.parse.quote(query, safe="")
        for opt in opts or []:
            path += "&o=" + urllib.parse.quote(opt, safe="")
        result = self._get_json(path)
        return result if isinstance(result, list) else []

    def get_change(self, change_id: str, opts: list[str] | None = None) -> dict:
        """`GET /changes/<id>?o=<opt>...` -> a single ChangeInfo dict."""
        path = f"/changes/{self._q(change_id)}"
        first = True
        for opt in opts or []:
            path += ("?o=" if first else "&o=") + urllib.parse.quote(opt, safe="")
            first = False
        return self._get_json(path)

    def get_related(self, change_id: str) -> list[dict]:
        """`GET /changes/<id>/revisions/current/related` -> the relation chain members
        (ordered), or an empty list when the change stands alone."""
        data = self._get_json(f"/changes/{self._q(change_id)}/revisions/current/related")
        changes = (data or {}).get("changes")
        return changes if isinstance(changes, list) else []

    def rebase(self, change_id: str, *, on_behalf_of_uploader: bool = True) -> dict:
        """`POST /changes/<id>/rebase` with
        `RebaseInput.rebase_on_behalf_of_uploader` -> the rebased ChangeInfo."""
        body = {"rebase_on_behalf_of_uploader": on_behalf_of_uploader}
        _status, text = self._request("POST", f"/changes/{self._q(change_id)}/rebase", body=body)
        return json.loads(_strip_xssi(text))

    def rebase_chain(self, change_id: str, *, on_behalf_of_uploader: bool = True) -> dict:
        """`POST /changes/<id>/rebase:chain` with
        `RebaseInput.rebase_on_behalf_of_uploader` -> the RebaseChainInfo."""
        body = {"rebase_on_behalf_of_uploader": on_behalf_of_uploader}
        _status, text = self._request(
            "POST", f"/changes/{self._q(change_id)}/rebase:chain", body=body
        )
        return json.loads(_strip_xssi(text))

    def submit(self, change_id: str) -> dict:
        """`POST /changes/<id>/submit` -> the submitted ChangeInfo."""
        _status, text = self._request("POST", f"/changes/{self._q(change_id)}/submit", body={})
        return json.loads(_strip_xssi(text))

    def set_review(
        self,
        change_id: str,
        *,
        message: str | None = None,
        labels: dict | None = None,
    ) -> dict:
        """`POST /changes/<id>/revisions/current/review` — post a comment and/or set label
        votes (e.g. `{"Autosubmit": 0}` to remove a land request). -> the ReviewResult."""
        body: dict = {}
        if message is not None:
            body["message"] = message
        if labels is not None:
            body["labels"] = labels
        _status, text = self._request(
            "POST", f"/changes/{self._q(change_id)}/revisions/current/review", body=body
        )
        return json.loads(_strip_xssi(text))

    @staticmethod
    def _q(change_id: str) -> str:
        """URL-encode a change id (`project~branch~Iabc` ids contain `~`)."""
        return urllib.parse.quote(str(change_id), safe="")
