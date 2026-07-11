"""rebar attestation substrate: DSSE envelope, pluggable signing schemes, per-kind policy.

Epic brilliant-curly-songbird (2fd4): one hardened, standards-based signing substrate
(DSSE PAE envelope + pluggable scheme registry + SSHSIG + HMAC-as-legacy) that the
identity and operation-cert epics both import.
"""

from __future__ import annotations

# Register the built-in HMAC-SHA256 scheme + its legacy-kind policy at import
# time so the substrate's default scheme is always live. Imported here at the
# end of the package init (after the package is otherwise importable) because
# ``hmac_legacy`` imports ``from rebar.attest import dsse, registry`` — those
# submodules must be importable before this runs.
from rebar.attest import hmac_legacy

hmac_legacy.register_legacy_schemes()
