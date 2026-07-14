"use strict";

// Viewer-request Lambda@Edge gate for a protected *.solutions.navateam.com
// CloudFront distribution. On every request:
//   - valid signed session cookie  → pass the request through to the origin.
//   - missing / invalid / expired  → 302 to the central auth host's /authorize,
//                                     carrying the original URL as return_to.
//
// Lambda@Edge forbids env vars and can't call SSM at viewer-request, so the
// cookie-signing secret + auth-host URL are BAKED in at deploy via the generated
// config.js (see config.js.tftpl). The bundle preserves the edge-gate/ + lib/
// layout so require("../lib/cookie") and require("./config") both resolve.

const cookie = require("../lib/cookie");

// Lazy so unit tests can exercise the pure helpers without the generated config.
let _cfg = null;
function config() {
  if (!_cfg) _cfg = require("./config");
  return _cfg;
}

// CloudFront delivers request cookies as an array of header objects, each
// value a "; "-joined cookie string. Pull out one cookie by name.
function extractCookie(headers, name) {
  const cookieHeaders = (headers && headers.cookie) || [];
  for (const h of cookieHeaders) {
    const parts = String(h.value || "").split(/;\s*/);
    for (const p of parts) {
      const eq = p.indexOf("=");
      if (eq > 0 && p.slice(0, eq) === name) return p.slice(eq + 1);
    }
  }
  return null;
}

// Build the 302-to-/authorize response, capturing the original https URL so the
// auth host can bounce the viewer straight back after sign-in.
function buildAuthRedirect(request, headers, authHostUrl) {
  const host = (headers && headers.host && headers.host[0] && headers.host[0].value) || "";
  const qs = request.querystring ? "?" + request.querystring : "";
  const returnTo = "https://" + host + (request.uri || "/") + qs;
  const location = authHostUrl + "/authorize?return_to=" + encodeURIComponent(returnTo);
  return {
    status: "302",
    statusDescription: "Found",
    headers: {
      location: [{ key: "Location", value: location }],
      "cache-control": [{ key: "Cache-Control", value: "no-store" }],
    },
  };
}

exports.handler = async (event) => {
  const cfg = config();
  const request = event.Records[0].cf.request;
  const headers = request.headers || {};

  const token = extractCookie(headers, cfg.cookieName);
  if (token) {
    const res = cookie.verify(token, cfg.signingSecret);
    if (res.valid) return request; // authenticated → straight to origin
  }
  return buildAuthRedirect(request, headers, cfg.authHostUrl);
};

// Pure helpers for unit tests (no baked config, no AWS).
exports._test = { extractCookie, buildAuthRedirect };
