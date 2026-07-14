"use strict";

// Central auth host for *.solutions.navateam.com (Option B).
// A regional Lambda (us-east-1) behind a Function URL, fronted by CloudFront at
// auth.solutions.navateam.com — the ONLY host registered with Google.
//
// Endpoints:
//   GET /authorize?return_to=<https *.solutions.navateam.com url>
//       → 302 to Google's consent screen, carrying a signed `state` that holds
//         the validated return_to (open-redirect-safe).
//   GET /_callback?code=..&state=..
//       → exchange code at Google's token endpoint, verify the ID token's
//         hd/email are navapbc.com, mint the HMAC session cookie scoped to
//         .solutions.navateam.com, 302 back to return_to. Non-navapbc → 403.
//   GET /_logout  → clear the session cookie.
//
// Unlike the edge gate, this Lambda is NOT edge-constrained: non-secret config
// arrives via env vars, and the two secrets (Google client_secret + cookie
// signing key) are read from SSM at RUNTIME — never baked into the artifact.

const crypto = require("crypto");
const cookie = require("../lib/cookie"); // bundle preserves auth-host/ + lib/ layout
// @aws-sdk/client-ssm is bundled in the nodejs20.x runtime; required lazily in
// getSecrets() so the pure logic loads (and unit-tests) without the SDK present.

// --- config (non-secret) from env -------------------------------------------
const CFG = {
  clientId: process.env.GOOGLE_CLIENT_ID,
  redirectUri: process.env.REDIRECT_URI, // https://auth.<domain>/_callback
  hostedDomain: process.env.HOSTED_DOMAIN, // navapbc.com
  cookieDomain: process.env.COOKIE_DOMAIN, // .solutions.navateam.com
  baseDomain: process.env.BASE_DOMAIN, // solutions.navateam.com (return_to allow-suffix)
  ttlSeconds: parseInt(process.env.SESSION_TTL_SECONDS || "43200", 10), // 12h
  googleSecretParam: process.env.GOOGLE_SECRET_PARAM, // SSM name
  cookieSecretParam: process.env.COOKIE_SECRET_PARAM, // SSM name
  region: process.env.AWS_REGION || "us-east-1",
  // Shared secret that CloudFront injects as an origin header; the Function URL
  // is auth_type=NONE, so this is what keeps the raw URL un-invocable by others.
  originSecret: process.env.ORIGIN_SECRET,
};

// Constant-time equality for the origin-secret header.
function secretEquals(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  const ab = Buffer.from(a);
  const bb = Buffer.from(b);
  return ab.length === bb.length && crypto.timingSafeEqual(ab, bb);
}

const GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth";
const GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token";
const STATE_TTL_SECONDS = 600; // a code-flow round trip is short; 10 min is generous

// --- secrets: fetched from SSM once per cold start, cached in module scope ---
let _secretsPromise = null;
function getSecrets() {
  if (!_secretsPromise) {
    const { SSMClient, GetParametersCommand } = require("@aws-sdk/client-ssm");
    const ssm = new SSMClient({ region: CFG.region });
    _secretsPromise = ssm
      .send(
        new GetParametersCommand({
          Names: [CFG.googleSecretParam, CFG.cookieSecretParam],
          WithDecryption: true,
        }),
      )
      .then((out) => {
        const byName = Object.fromEntries((out.Parameters || []).map((p) => [p.Name, p.Value]));
        const clientSecret = byName[CFG.googleSecretParam];
        const cookieSecret = byName[CFG.cookieSecretParam];
        if (!clientSecret || !cookieSecret) {
          throw new Error("auth-host: missing secret(s) in SSM");
        }
        return { clientSecret, cookieSecret };
      })
      .catch((err) => {
        _secretsPromise = null; // allow retry on the next invocation
        throw err;
      });
  }
  return _secretsPromise;
}

// --- signed state (binds return_to to the round trip) -----------------------
// Independent of the session cookie format; carries return_to + freshness and
// is HMAC'd with the same cookie secret under a distinct purpose label.
function signState(returnTo, secret, now) {
  const payload = JSON.stringify({
    p: "state",
    rt: returnTo,
    n: crypto.randomBytes(12).toString("hex"),
    exp: now + STATE_TTL_SECONDS,
  });
  const sig = crypto.createHmac("sha256", secret).update(payload).digest();
  return cookie._b64urlEncode(payload) + "." + cookie._b64urlEncode(sig);
}

function verifyState(state, secret, now) {
  if (typeof state !== "string" || state.indexOf(".") < 0) return null;
  const dot = state.indexOf(".");
  const body = state.slice(0, dot);
  const sigB64 = state.slice(dot + 1);
  let payloadJson;
  try {
    payloadJson = cookie._b64urlDecode(body).toString("utf8");
  } catch (e) {
    return null;
  }
  const expected = crypto.createHmac("sha256", secret).update(payloadJson).digest();
  let provided;
  try {
    provided = cookie._b64urlDecode(sigB64);
  } catch (e) {
    return null;
  }
  if (provided.length !== expected.length || !crypto.timingSafeEqual(provided, expected)) {
    return null;
  }
  let payload;
  try {
    payload = JSON.parse(payloadJson);
  } catch (e) {
    return null;
  }
  if (payload.p !== "state" || now > payload.exp) return null;
  return payload;
}

// --- return_to allow-list: only https *.<baseDomain> -----------------------
function isAllowedReturnTo(raw) {
  if (!raw) return false;
  let u;
  try {
    u = new URL(raw);
  } catch (e) {
    return false;
  }
  if (u.protocol !== "https:") return false;
  const h = u.hostname;
  return h === CFG.baseDomain || h.endsWith("." + CFG.baseDomain);
}

// --- ID-token handling ------------------------------------------------------
// The code was exchanged server-to-server with Google over TLS, so per Google's
// guidance the returned id_token can be trusted without re-verifying its
// signature; we still defensively check aud/iss and read hd/email.
function decodeJwtPayload(idToken) {
  const parts = String(idToken).split(".");
  if (parts.length < 2) return null;
  try {
    return JSON.parse(cookie._b64urlDecode(parts[1]).toString("utf8"));
  } catch (e) {
    return null;
  }
}

function isNavapbcIdentity(claims) {
  if (!claims) return false;
  const audOk = claims.aud === CFG.clientId;
  const issOk = claims.iss === "accounts.google.com" || claims.iss === "https://accounts.google.com";
  const email = (claims.email || "").toLowerCase();
  const emailOk = claims.email_verified === true && email.endsWith("@" + CFG.hostedDomain);
  const hdOk = claims.hd === CFG.hostedDomain; // Workspace org claim (defense-in-depth)
  return audOk && issOk && emailOk && hdOk;
}

// --- responses --------------------------------------------------------------
function redirect(location, cookies) {
  return { statusCode: 302, headers: { location, "cache-control": "no-store" }, cookies: cookies || [] };
}

function htmlResponse(statusCode, title, message) {
  const esc = (s) =>
    String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const body =
    '<!doctype html><html lang="en"><head><meta charset="utf-8">' +
    '<meta name="viewport" content="width=device-width,initial-scale=1">' +
    "<title>" +
    esc(title) +
    "</title>" +
    "<style>body{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#1a1a1a}" +
    "h1{font-size:1.4rem}code{background:#f0f0f0;padding:.1em .3em;border-radius:3px}</style></head>" +
    "<body><h1>" +
    esc(title) +
    "</h1><p>" +
    esc(message) +
    "</p></body></html>";
  return { statusCode, headers: { "content-type": "text/html; charset=utf-8", "cache-control": "no-store" }, body };
}

function setCookieString(value, maxAge) {
  // SameSite=Lax so the post-Google top-level redirect carries the cookie.
  return (
    cookie.COOKIE_NAME +
    "=" +
    value +
    "; Domain=" +
    CFG.cookieDomain +
    "; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age=" +
    maxAge
  );
}

// --- handler ----------------------------------------------------------------
exports.handler = async (event) => {
  const method = (event.requestContext && event.requestContext.http && event.requestContext.http.method) || "GET";
  const path = event.rawPath || "/";
  const query = new URLSearchParams(event.rawQueryString || "");
  const now = Math.floor(Date.now() / 1000);

  if (method !== "GET") return htmlResponse(405, "Method not allowed", "Use GET.");

  // Only CloudFront knows the origin secret (injected as a custom origin
  // header). A direct hit on the raw Function URL lacks it → 403.
  if (CFG.originSecret) {
    const headers = event.headers || {};
    const presented = headers["x-origin-auth"] || headers["X-Origin-Auth"] || "";
    if (!secretEquals(presented, CFG.originSecret)) {
      return htmlResponse(403, "Forbidden", "Direct access to this endpoint is not allowed.");
    }
  }

  try {
    if (path === "/authorize") {
      const returnTo = query.get("return_to");
      if (!isAllowedReturnTo(returnTo)) {
        return htmlResponse(400, "Invalid return target", "return_to must be an https URL on this domain.");
      }
      const { cookieSecret } = await getSecrets();
      const state = signState(returnTo, cookieSecret, now);
      const authUrl = new URL(GOOGLE_AUTH_URL);
      authUrl.searchParams.set("client_id", CFG.clientId);
      authUrl.searchParams.set("redirect_uri", CFG.redirectUri);
      authUrl.searchParams.set("response_type", "code");
      authUrl.searchParams.set("scope", "openid email profile");
      authUrl.searchParams.set("hd", CFG.hostedDomain); // hint Google to the org
      authUrl.searchParams.set("access_type", "online");
      authUrl.searchParams.set("prompt", "select_account");
      authUrl.searchParams.set("state", state);
      return redirect(authUrl.toString());
    }

    if (path === "/_callback") {
      const error = query.get("error");
      if (error) return htmlResponse(403, "Sign-in cancelled", "Google returned: " + error);
      const code = query.get("code");
      const state = query.get("state");
      const { clientSecret, cookieSecret } = await getSecrets();

      const st = verifyState(state, cookieSecret, now);
      if (!st || !isAllowedReturnTo(st.rt)) {
        return htmlResponse(400, "Invalid or expired sign-in", "Please start again from the dashboard.");
      }
      if (!code) return htmlResponse(400, "Missing authorization code", "Please start again.");

      // Back-channel code exchange (server-to-server, over TLS).
      const tokenRes = await fetch(GOOGLE_TOKEN_URL, {
        method: "POST",
        headers: { "content-type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          code,
          client_id: CFG.clientId,
          client_secret: clientSecret,
          redirect_uri: CFG.redirectUri,
          grant_type: "authorization_code",
        }),
      });
      if (!tokenRes.ok) {
        return htmlResponse(502, "Sign-in failed", "Could not complete the exchange with Google.");
      }
      const tokens = await tokenRes.json();
      const claims = decodeJwtPayload(tokens.id_token);

      if (!isNavapbcIdentity(claims)) {
        return htmlResponse(
          403,
          "Access denied",
          "This dashboard is restricted to @" +
            CFG.hostedDomain +
            " accounts. " +
            "You are signed in to Google as " +
            ((claims && claims.email) || "an unknown account") +
            ".",
        );
      }

      const session = cookie.sign({ sub: claims.sub, email: claims.email }, cookieSecret, CFG.ttlSeconds, now);
      return redirect(st.rt, [setCookieString(session, CFG.ttlSeconds)]);
    }

    if (path === "/_logout") {
      const returnTo = query.get("return_to");
      const cleared = [setCookieString("", 0)];
      if (isAllowedReturnTo(returnTo)) return redirect(returnTo, cleared);
      return { ...htmlResponse(200, "Signed out", "Your session has been cleared."), cookies: cleared };
    }

    return htmlResponse(404, "Not found", "Unknown endpoint.");
  } catch (err) {
    console.error("auth-host error:", err && err.stack ? err.stack : err);
    return htmlResponse(500, "Server error", "Something went wrong. Please try again.");
  }
};

// Pure helpers exported for unit tests (no AWS dependency).
exports._test = { signState, verifyState, isAllowedReturnTo, decodeJwtPayload, isNavapbcIdentity, CFG };
