#!/usr/bin/env python3
"""SDET audit P3-2: Jira VCR-cassette replay loader.

Per the audit MODIFY verdict, a live Jira tenant introduces external-dep
flake — VCR-style cassette replay against recorded interactions is the
recommended pattern. This module is the scaffolded minimum: load a cassette
file (JSONL) and serve recorded responses for matching request signatures.

Cassette JSONL shape (one record per line):
  {
    "request": {
      "method": "GET",
      "url": "https://example.atlassian.net/rest/api/3/issue/DIG-123",
      "body": null
    },
    "response": {
      "status_code": 200,
      "headers": {"Content-Type": "application/json"},
      "body_json": {"key": "DIG-123", "fields": {"summary": "x"}}
    }
  }

Stdlib only — no `vcr.py` or `responses` package dependency. Tests should
patch the Jira-bridge HTTP layer with `replay(...)` to get a recorded
response by (method, url) signature.

Today's scope is the SCAFFOLD only: cassette loader, signature matching,
and a 401/Retry-After scenario sample. Live recording of cassettes from a
real Jira tenant is a multi-day follow-up.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any


class Cassette:
    """Replay a recorded Jira-bridge interaction set."""

    def __init__(self, records: list[dict]):
        self.records = records
        self._cursor: dict[tuple[str, str], int] = {}

    @classmethod
    def from_file(cls, path: str) -> Cassette:
        records: list[dict] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                records.append(json.loads(line))
        return cls(records)

    def replay(self, method: str, url: str, body: Any | None = None) -> dict:
        """Return the next recorded response for (method, url).

        Each (method, url) pair has its own cursor so multiple recorded
        responses (e.g., 429 then 200 on retry) replay in order.

        Raises KeyError when no matching recorded request is found.
        """
        key = (method.upper(), url)
        idx = self._cursor.get(key, 0)
        for offset, rec in enumerate(self.records[idx:]):
            req = rec.get("request", {})
            if (
                req.get("method", "").upper() == method.upper()
                and req.get("url") == url
            ):
                self._cursor[key] = idx + offset + 1
                return rec.get("response", {})
        raise KeyError(f"No recorded response for {method} {url}")


def _self_test() -> int:
    """Minimal self-test: load a synthetic cassette, replay, verify ordering."""
    import tempfile

    sample = [
        {
            "request": {"method": "GET", "url": "https://x/issue/1"},
            "response": {
                "status_code": 429,
                "headers": {"Retry-After": "1"},
                "body_json": {"errorMessages": ["slow down"]},
            },
        },
        {
            "request": {"method": "GET", "url": "https://x/issue/1"},
            "response": {
                "status_code": 200,
                "headers": {"Content-Type": "application/json"},
                "body_json": {"key": "DIG-1"},
            },
        },
    ]

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for r in sample:
            f.write(json.dumps(r) + "\n")
        path = f.name

    try:
        c = Cassette.from_file(path)
        first = c.replay("GET", "https://x/issue/1")
        assert first["status_code"] == 429, f"expected 429, got {first}"
        assert first["headers"]["Retry-After"] == "1"

        second = c.replay("GET", "https://x/issue/1")
        assert second["status_code"] == 200, f"expected 200, got {second}"
        assert second["body_json"]["key"] == "DIG-1"

        # Third replay should raise — no more records.
        raised = False
        try:
            c.replay("GET", "https://x/issue/1")
        except KeyError:
            raised = True
        assert raised, "expected KeyError on exhausted cassette"
        print("Cassette self-test: PASS")
        return 0
    finally:
        os.unlink(path)


if __name__ == "__main__":
    sys.exit(_self_test())
