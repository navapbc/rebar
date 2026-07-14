variable "name" {
  description = "Resource prefix; the Lambda@Edge function and role are named <name>-sso-gate."
  type        = string
}

variable "account_id" {
  description = "AWS account id, used to scope the (multi-region) Lambda@Edge log-group ARN."
  type        = string
}

variable "source_root" {
  description = "Path to the auth source directory containing edge-gate/ and lib/cookie.js. Single-sourced with the auth host so sign/verify can never drift."
  type        = string
}

variable "signing_secret" {
  description = "Cookie-signing HMAC key. BAKED into the bundle — Lambda@Edge has no env vars and can't read SSM at viewer-request. Pass the SSM SecureString value."
  type        = string
  sensitive   = true
}

variable "auth_host_url" {
  description = "Base URL of the central auth host (https://auth.<domain>). Unauthenticated viewers are 302'd to <auth_host_url>/authorize."
  type        = string
}

variable "cookie_name" {
  description = "Name of the shared session cookie."
  type        = string
  default     = "__Secure-sso"
}
