"use strict";

// Shared session-cookie library — HMAC-signs and verifies the SSO session
// payload {sub, email, exp}. Used by BOTH the central auth host (which mints
// the cookie after a successful Google code flow) and every subdomain's
// viewer-request gate (which verifies it). Both sides bake/read the SAME
// signing secret so a signature minted by the auth host verifies at any gate.
//
// Format:  base64url(payloadJSON) "." base64url(HMAC_SHA256(payloadJSON, key))
// — a compact, self-contained token (a minimal JWS-alike). No external deps so
// the file drops straight into a Lambda@Edge bundle (no env vars, no layers).
//
// Security properties:
//  - HMAC-SHA256 integrity (tamper of payload OR signature => reject).
//  - Constant-time signature compare (crypto.timingSafeEqual) — no early-exit
//    timing oracle, and length-mismatch is treated as a non-match, not a throw.
//  - Hard expiry on `exp` (unix seconds), with a small symmetric clock-skew
//    tolerance so a few seconds of drift between hosts doesn't spuriously 401.

const crypto = require("crypto");

// `__Secure-` prefix: the browser enforces the cookie was set with Secure over
// HTTPS. We CAN'T use `__Host-` here — it forbids a Domain attribute, but
// cross-subdomain SSO requires Domain=.solutions.navateam.com.
const COOKIE_NAME = "__Secure-sso";
const DEFAULT_SKEW_SECONDS = 60; // tolerate ±60s of clock drift between hosts

function b64urlEncode(buf) {
  return Buffer.from(buf).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(str) {
  // Restore standard base64 padding before decoding.
  const pad = str.length % 4 === 0 ? "" : "=".repeat(4 - (str.length % 4));
  return Buffer.from(str.replace(/-/g, "+").replace(/_/g, "/") + pad, "base64");
}

function hmac(payloadJson, secret) {
  return crypto.createHmac("sha256", secret).update(payloadJson, "utf8").digest();
}

// Sign a session. `claims` must include {sub, email}; `ttlSeconds` sets the
// hard lifetime. `nowSeconds` is injectable for deterministic tests.
function sign(claims, secret, ttlSeconds, nowSeconds) {
  if (!secret) throw new Error("cookie.sign: missing signing secret");
  if (!claims || !claims.sub || !claims.email) {
    throw new Error("cookie.sign: claims require sub and email");
  }
  const now = nowSeconds == null ? Math.floor(Date.now() / 1000) : nowSeconds;
  const payload = { sub: claims.sub, email: claims.email, exp: now + ttlSeconds };
  const payloadJson = JSON.stringify(payload);
  const sig = hmac(payloadJson, secret);
  return b64urlEncode(payloadJson) + "." + b64urlEncode(sig);
}

// Verify a token. Returns {valid:true, payload} on success, or
// {valid:false, reason} on any failure. Never throws on malformed input —
// callers (especially the edge gate) want a boolean decision, not an exception.
function verify(token, secret, opts) {
  const skew = opts && opts.skewSeconds != null ? opts.skewSeconds : DEFAULT_SKEW_SECONDS;
  const now = opts && opts.nowSeconds != null ? opts.nowSeconds : Math.floor(Date.now() / 1000);

  if (!secret) return { valid: false, reason: "no-secret" };
  if (typeof token !== "string" || token.indexOf(".") < 0) {
    return { valid: false, reason: "malformed" };
  }

  const dot = token.indexOf(".");
  const payloadB64 = token.slice(0, dot);
  const sigB64 = token.slice(dot + 1);
  if (!payloadB64 || !sigB64) return { valid: false, reason: "malformed" };

  let payloadJson;
  try {
    payloadJson = b64urlDecode(payloadB64).toString("utf8");
  } catch (e) {
    return { valid: false, reason: "malformed" };
  }

  // Recompute the expected signature and compare in constant time. A length
  // mismatch (e.g. a truncated sig) must not throw timingSafeEqual — treat it
  // as a plain non-match.
  const expected = hmac(payloadJson, secret);
  let provided;
  try {
    provided = b64urlDecode(sigB64);
  } catch (e) {
    return { valid: false, reason: "bad-signature" };
  }
  if (provided.length !== expected.length || !crypto.timingSafeEqual(provided, expected)) {
    return { valid: false, reason: "bad-signature" };
  }

  let payload;
  try {
    payload = JSON.parse(payloadJson);
  } catch (e) {
    return { valid: false, reason: "malformed" };
  }
  if (typeof payload.exp !== "number") return { valid: false, reason: "no-exp" };
  if (now > payload.exp + skew) return { valid: false, reason: "expired" };

  return { valid: true, payload };
}

module.exports = {
  sign,
  verify,
  COOKIE_NAME,
  DEFAULT_SKEW_SECONDS,
  // exported for tests / reuse
  _b64urlEncode: b64urlEncode,
  _b64urlDecode: b64urlDecode,
};
