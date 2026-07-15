"""rebar attestation substrate: DSSE envelope, pluggable signing schemes, per-kind policy.

Epic brilliant-curly-songbird (2fd4): one hardened, standards-based signing substrate
(DSSE PAE envelope + pluggable scheme registry + SSHSIG) that the identity and
operation-cert epics both import.

Story 8f1d (contract phase): the legacy symmetric HMAC-SHA256 scheme has been RETIRED for
the op-cert kinds (``plan-review`` / ``completion-verifier``). It is no longer registered
in the scheme/policy registry, so those kinds resolve ONLY the asymmetric op-cert
(SSHSIG/DSSE) scheme and can neither be signed nor accepted as HMAC. The generic HMAC
utility (``signing.compute_signature`` / ``.signing-key``) survives for non-op-cert
consumers, and ``rebar.authorship.v1`` (already asymmetric) is unaffected.
"""

from __future__ import annotations

# Register the built-in signing schemes + their per-kind policies at import time so the
# substrate's schemes are always live. Imported here at the end of the package init
# (after the package is otherwise importable) because each submodule imports
# ``from rebar.attest import dsse, registry`` — those submodules must be importable
# before this runs.
from rebar.attest import authorship, opcert, sshsig

sshsig.register_sshsig_scheme()
authorship.register_authorship_policy()
opcert.register_opcert_policy()
