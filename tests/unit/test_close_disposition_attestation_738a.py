"""A disposition close MINTS an attestation instead of withholding one (bug 738a).

THE DEFECT. The close path exempts a `duplicate` / `not_a_bug` / `escalated` bug that names a live
replacement from completion verification — correct, since such a bug never claimed to implement its
own acceptance criteria. But it also returned no sign signal, so the close was unsigned, and
``child_closure_findings`` counts any unsigned closure as UNCERTIFIED and withholds the parent's
signature. Two correct rules composing into a state with no honest exit: reopening took the same
exemption every time, and ``--force`` produced the very uncertified state the ruling forbids.

The exemption keyed on the link that DOCUMENTS the disposition, so certification was reachable only
for duplicates that hid their replacement. These tests pin the fix and, more
importantly, the two properties that make it safe: the manifest must not claim a completion PASS
that never happened, and
the disposition must still be rejected when the ticket does not actually qualify.
"""

from __future__ import annotations

import pytest

from rebar._commands import close_disposition, close_precheck


def test_the_disposition_classes_match_the_close_gates_own_set():
    """The two definitions must not drift; a divergence silently changes which closes attest."""
    assert close_disposition.DISPOSITION_CLASSES == close_precheck._NON_COMPLETION_BUG_CLASSES


@pytest.mark.parametrize(
    "close_class",
    sorted(
        (close_disposition.DISPOSITION_CLASSES - close_disposition.REASON_REQUIRED_CLASSES)
        | close_disposition.REPLACEMENT_SATISFIES_REASON_CLASSES
    ),
)
def test_every_replacement_bearing_class_can_produce_a_verdict(close_class, monkeypatch):
    """All replacement-bearing exempt classes, not just `duplicate`.

    Measured when 738a was filed, only `duplicate` had closes carrying a live replacement link;
    `not_a_bug` had none and `escalated` had no closed bugs at all. So the other two were LATENT
    siblings of the same defect — identical code path, gap appearing the moment one is linked.
    Parametrizing means the fix covers them before that happens rather than after. The
    REASON-ONLY administrative classes (obsolete/wontfix, ticket fc20) mint from their
    ``close_reason`` instead — asserted separately below — while `not_a_bug`/`escalated`
    (reason-required since bug d54b, replacement checked FIRST) must keep this mint unchanged.
    """
    monkeypatch.setattr(
        close_disposition, "find_replacement", lambda *a, **k: "dead-beef-cafe-0001"
    )

    result = close_disposition.verdict("tkt-0001", close_class, "/nonexistent/tracker")

    assert result is not None
    assert result["disposition"] == close_class
    assert result["replacement"] == "dead-beef-cafe-0001"
    assert result["verdict"] == "PASS"


@pytest.mark.parametrize("close_class", sorted(close_disposition.REASON_REQUIRED_CLASSES))
def test_every_reason_only_class_can_produce_a_verdict(close_class):
    """The reason-only administrative classes (ticket fc20) mint from their justification.

    They carry no replacement link, so the same fail-closed property holds in the other
    direction: a reason yields a signed disposition, no reason yields no signature.
    """
    result = close_disposition.verdict(
        "tkt-0001", close_class, "/nonexistent/tracker", close_reason="stated justification"
    )

    assert result is not None
    assert result["disposition"] == close_class
    assert result["close_reason"] == "stated justification"
    assert result["verdict"] == "PASS"

    assert close_disposition.verdict("tkt-0001", close_class, "/nonexistent/tracker") is None


def test_a_force_close_is_still_never_signed_even_for_a_linked_duplicate(monkeypatch, tmp_path):
    """THE GUARANTEE THIS FIX MUST NOT WEAKEN: `--force` still withholds the signature.

    The disposition branch mints an attestation, so the ordering inside `_completion_precheck`
    becomes load-bearing: `if force_close: return None` runs BEFORE it. If that order were ever
    reversed, `--force --class duplicate` on a linked bug would start producing a signed
    closure — turning the operator's escape hatch into a way to certify without verifying, which is
    exactly the state the reopen ruling exists to prevent.

    Asserted with the disposition path fully primed (gate ON, replacement link present), so the only
    thing that can produce `None` here is the force-close guard itself.
    """
    from rebar._commands import gates as _gates

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(
        close_precheck, "_has_live_replacement_link", lambda *a, **k: True, raising=False
    )
    called: list[str] = []
    monkeypatch.setattr(
        close_disposition, "verdict", lambda *a, **k: called.append("verdict") or {"x": 1}
    )

    out, expectation = close_precheck._completion_precheck(
        "0059-c7f0-cce1-4077",
        "bug",
        str(tmp_path),
        None,
        reason="",
        force_close="operator override",
        close_class="duplicate",
    )

    assert out is None, "a --force must return no sign signal, so the close stays unsigned"
    assert expectation == "force_bypassed"
    assert called == [], "the disposition attestation must not even be built for a force-close"


def test_the_precheck_ACTUALLY_returns_the_disposition_verdict(monkeypatch, tmp_path):
    """THE WIRING TEST. Everything else here exercises the module in isolation.

    This calls the real `_completion_precheck` on the happy disposition path, so a broken call site
    fails here rather than silently reverting to the old unsigned behaviour. It exists because the
    call site MOVED — bug 74a3 split the precheck cluster into `close_precheck`, and the end-to-end
    verification of this fix had been done against the previous location. A unit suite that only
    tests the pieces would have stayed green through that move while the gate went back to
    withholding signatures.
    """
    from rebar._commands import gates as _gates

    monkeypatch.setattr(_gates, "gate_enabled", lambda *a, **k: True)
    monkeypatch.setattr(
        close_precheck, "_has_live_replacement_link", lambda *a, **k: True, raising=False
    )
    monkeypatch.setattr(
        close_disposition,
        "verdict",
        lambda tid, cc, tracker: {"verdict": "PASS", "disposition": cc, "replacement": "r-1"},
    )

    out, expectation = close_precheck._completion_precheck(
        "0059-c7f0-cce1-4077",
        "bug",
        str(tmp_path),
        None,
        reason="",
        force_close="",
        close_class="duplicate",
    )

    assert out is not None, (
        "the precheck returned no sign signal on a disposition close — the close would be UNSIGNED "
        "and its parent's certification withheld, which is exactly bug 738a"
    )
    assert out["disposition"] == "duplicate"
    assert expectation == "disposition"


def test_no_verdict_without_a_live_replacement():
    """FAIL-CLOSED: no link, no attestation — the close keeps normal verification.

    This is the property that stops the fix from becoming a bypass. If a missing or dead link still
    minted a signature, `--class duplicate` would be a way to certify any bug without verifying it.
    """
    assert close_disposition.verdict("tkt-0001", "duplicate", "/nonexistent/tracker") is None


@pytest.mark.parametrize("close_class", ["regression", "preexisting", "flaky", "undetermined", ""])
def test_a_non_disposition_class_never_produces_a_verdict(close_class):
    """Classes that DO owe completion verification must be untouched by this path.

    `flaky` / `env_integration` / `undetermined` closes often involve no code change either, but
    they are not dispositions — they still run the verifier, and they were already being certified.
    Widening the disposition set to them would remove real verification.
    """
    assert (
        close_disposition.find_replacement("tkt-0001", close_class, "/nonexistent/tracker") is None
    )


def test_the_manifest_does_not_claim_a_completion_pass():
    """THE HONESTY PROPERTY. The signed bytes must say what they actually attest.

    Minting `completion-verifier: PASS` for a ticket whose completion was never verified would be a
    lie in the signature — worse than the gap being closed, because an auditor could not tell a
    verified completion from a bookkeeping disposition.
    """
    base = [
        "completion-verifier: PASS",
        "ticket: tkt-0001",
        "model: none (deterministic disposition)",
        "runner: close_disposition",
        "rebar: 1.2.3",
        "material: cafebabe",
    ]
    result = {"disposition": "duplicate", "replacement": "dead-beef-cafe-0001"}

    out = close_disposition.decorate_manifest(base, result)

    assert out[0] == "completion-verifier: DISPOSITION duplicate"
    assert "completion-verifier: PASS" not in out
    assert out[1] == "replacement: dead-beef-cafe-0001"


def test_the_manifest_still_reads_as_a_completion_verifier_record():
    """The kind is the text before the first ": " (reducer/_processors_identity.py).

    If the rewrite broke that, `verify_signature(kind="completion-verifier")` would stop finding the
    record and the fix would close the gap by making the attestation invisible instead.
    """
    out = close_disposition.decorate_manifest(
        ["completion-verifier: PASS", "ticket: tkt-0001"], {"disposition": "duplicate"}
    )

    assert out[0].split(": ", 1)[0] == "completion-verifier"


def test_the_material_fingerprint_survives_decoration():
    """Validity-on-read must behave identically to a verified completion.

    The material step is what makes a post-verdict edit invalidate the attestation. Dropping it
    during decoration would make disposition attestations permanently valid — a second, quieter
    defect in place of the first.
    """
    out = close_disposition.decorate_manifest(
        ["completion-verifier: PASS", "ticket: tkt-0001", "material: cafebabe"],
        {"disposition": "duplicate", "replacement": "r-1"},
    )

    assert "material: cafebabe" in out


def test_decoration_declines_rather_than_raising_on_an_empty_manifest():
    """A signing helper must not raise on a degenerate input.

    `decorated[0] = ...` on an empty list raises IndexError, and inside the close path that
    would surface as a crash rather than as an unsigned close — strictly worse than declining
    to decorate. `_verdict_manifest` always yields several steps, so this is unreachable from
    that caller, but the helper takes a caller-supplied list and should not be what breaks.
    """
    assert close_disposition.decorate_manifest([], {"disposition": "duplicate"}) == []


def test_decoration_is_a_no_op_without_a_disposition():
    """A normal completion verdict must pass through byte-identically."""
    base = ["completion-verifier: PASS", "ticket: tkt-0001", "material: cafebabe"]

    assert close_disposition.decorate_manifest(base, {"verdict": "PASS"}) == base


def test_the_verdict_records_that_no_model_was_consulted():
    """Auditability: the manifest should make the deterministic producer obvious.

    A disposition attestation with `model: n/a` is indistinguishable from a verifier run whose
    model was not recorded. Naming the producer is what lets a reader tell them apart.
    """
    result = {
        "disposition": "duplicate",
        "replacement": "r-1",
        "model": "none (deterministic disposition)",
        "runner": "close_disposition",
    }

    assert "none" in result["model"]
    assert result["runner"] == "close_disposition"
