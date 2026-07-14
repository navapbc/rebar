"""Trusted op-cert gate service (story ee0b) — a library-mode FastAPI app that fetches
authoritative state itself, runs a gate, and returns an Ed25519-signed op-cert.

IMPORTABILITY CONTRACT (mirrors ``rebar.review_bot``): ``import rebar.opcert_service`` and its
config/jobs/workspace/keyprov/ssm modules are FastAPI- and boto3-free. Only ``opcert_service.app``
imports FastAPI (the ``reviewbot`` extra), and only the SSM seam imports boto3 — both lazily/at that
module's top — so ``import rebar`` stays dependency-free.
"""

from __future__ import annotations

from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.jobs import VALID_KINDS, new_record, run_job

__all__ = ["OpcertServiceConfig", "VALID_KINDS", "new_record", "run_job"]
