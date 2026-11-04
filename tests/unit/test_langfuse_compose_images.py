"""The ephemeral Langfuse stack's MinIO image must stay anonymously pullable and immutable.

Bug ``4245-9f90-9d32-44c0`` (``unkempt-amaranth-archerfish``), following
``6d98-28f4-946a-4016`` (``stoppable-sorry-budgie``).

THE DEFECT. ``Live External Integration``'s ``langfuse tracing round-trip`` job failed on every
scheduled run from 2026-09-28 at ``docker compose -f docker-compose.langfuse.yml up -d`` with
``minio Error unauthorized``. MinIO has now withdrawn public images twice: Docker Hub's
``minio/minio`` went away (``6d98``), and the ``quay.io/minio/minio`` repository that replaced it
answers 401 to anonymous API and registry requests. A pinned release tag on a registry that
stops serving it is no pin at all.

THE CHOSEN IMAGE. ``cgr.dev/chainguard/minio`` is what upstream Langfuse's own
``docker-compose.yml`` runs for this service, with the same ``sh`` entrypoint and bucket-creating
command. Chainguard's free tier publishes only ``latest`` and ``latest-dev``, and
``scripts/check_uv_pin.py`` rejects ``:latest`` even when a digest follows it, so the only form
that is both pullable and gate-compliant is a bare index digest. That is also the strongest pin:
the weekly run cannot change images without a commit.

Network reachability cannot be asserted offline. These tests pin the decision so that a revert to
a withdrawn registry, or to a floating tag, fails here rather than on the next weekly run.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO_ROOT / "docker-compose.langfuse.yml"
CHAINGUARD_MINIO_DIGEST_PIN = re.compile(r"^cgr\.dev/chainguard/minio@sha256:[0-9a-f]{64}$")


def test_langfuse_minio_image_is_chainguard_digest_pin() -> None:
    """MinIO comes from Chainguard's public registry, pinned by an immutable digest."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    image = compose["services"]["minio"]["image"]
    assert CHAINGUARD_MINIO_DIGEST_PIN.fullmatch(image), (
        f"docker-compose.langfuse.yml minio image is {image!r}; expected "
        "cgr.dev/chainguard/minio@sha256:<64 hex>. quay.io/minio/minio and docker.io/minio/minio "
        "no longer serve anonymous pulls."
    )


def test_langfuse_minio_image_carries_no_tag() -> None:
    """No tag rides alongside the digest: Chainguard's only free tag is the floating ``latest``."""
    compose = yaml.safe_load(COMPOSE_FILE.read_text(encoding="utf-8"))
    repository = compose["services"]["minio"]["image"].partition("@")[0]
    last_segment = repository.rsplit("/", 1)[-1]
    assert ":" not in last_segment, (
        f"minio image repository {repository!r} carries a tag; pin by bare digest instead"
    )
