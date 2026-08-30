"""Tier-1's verifier-prompt candidate concept (ticket presolar-finable-binturong /
53ab-bdf6-de1c-4bb1) -- deliberately a DIFFERENT concept from Pass-3's
:class:`rebar.llm.evals.plan_replay.candidates.Candidate` (a per-criterion
threshold/posture OVERLAY registry keyed by NAME). Tier-1's candidate swaps the Pass-2
VERIFIER's prompt/questions -- a different axis entirely -- so it is resolved from a
bare filesystem PATH, not a registry key, and there is no ``CANDIDATES`` dict to add
entries to.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class VerifierCandidate:
    """One Pass-2 verifier-prompt candidate.

    ``prompt_path`` is a path to a project-override verifier prompt file to install
    ahead of the packaged production prompt (see
    :func:`rebar.llm.evals.plan_replay.tier1.build_candidate_runner`). ``None`` (the
    default) means production's shipped/packaged prompt is used unmodified -- the
    ``"current"`` reproduction run, whose agreement against stored answers IS the Pass-2
    noise floor at temperature 0.
    """

    prompt_path: str | None = None


def load_verifier_candidate(path_or_none: str | None) -> VerifierCandidate:
    """Resolve a ``--candidate`` CLI value to a :class:`VerifierCandidate`.

    ``None`` -> the reproduction run (production's prompt, unmodified). A non-``None``
    path must exist and be a readable file; raises :class:`FileNotFoundError` otherwise
    -- a typo'd path must fail loudly, never silently fall back to production's prompt.
    """
    if path_or_none is None:
        return VerifierCandidate(prompt_path=None)
    path = Path(path_or_none)
    if not path.is_file():
        raise FileNotFoundError(f"verifier candidate prompt not found: {path_or_none!r}")
    return VerifierCandidate(prompt_path=str(path))
