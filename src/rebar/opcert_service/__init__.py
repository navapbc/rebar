"""Trusted op-cert gate service (story ee0b) — a library-mode FastAPI app that fetches
authoritative state itself, runs a gate, and returns an Ed25519-signed op-cert.

IMPORTABILITY CONTRACT (mirrors ``rebar.review_bot``): ``import rebar.opcert_service`` and its
config/jobs/workspace/keyprov modules are FastAPI- and boto3-free. Only ``opcert_service.app``
imports FastAPI (the ``reviewbot`` extra), at that module's top — so ``import rebar`` stays
dependency-free. The signing key is composed ONCE at startup from a deployment-materialized FILE
(story 6f14; see :mod:`.keyprov`), so the app runtime no longer needs boto3/SSM at all.
"""

from __future__ import annotations

from rebar.opcert_service.config import OpcertServiceConfig
from rebar.opcert_service.jobs import VALID_KINDS, new_record, run_job
from rebar.opcert_service.keyprov import OpcertKeyError, OpcertSigner, compose_signer

__all__ = [
    "VALID_KINDS",
    "OpcertKeyError",
    "OpcertServiceConfig",
    "OpcertSigner",
    "compose_signer",
    "new_record",
    "run_job",
]
