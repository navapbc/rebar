#!/usr/bin/env python3
"""Print a named diagnostic for runner process exit statuses."""

from __future__ import annotations

import argparse

_FAILURE_CAUSES = {
    124: "timeout: wall-clock bound expired",
    137: "SIGKILL: process was killed, likely OOM on CI runners",
    139: "SIGSEGV: process crashed with a segmentation fault",
}


def classify(status: int) -> str:
    if status == 0:
        return "success: runner exited with status 0"
    return _FAILURE_CAUSES.get(status, f"failure: runner exited with status {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("status", type=int, help="runner process exit status")
    args = parser.parse_args()
    print(classify(args.status))
    return 0 if args.status == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
