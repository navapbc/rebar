variable "github_token" {
  type        = string
  description = <<-EOT
    GitHub token with Administration:write on navapbc/rebar, used to manage
    rulesets. PREFER sourcing it from the GITHUB_TOKEN environment variable
    (the provider reads GITHUB_TOKEN automatically when this is null) so the
    secret never lands in a .tfvars file or state. Passing it as a sensitive
    var is supported but discouraged. NEVER commit a token.
  EOT
  sensitive   = true
  default     = null
}

variable "repository" {
  type        = string
  description = "The repository to lock (under the navapbc owner)."
  default     = "rebar"
}

variable "deploy_key_title" {
  type        = string
  description = <<-EOT
    Title of the Gerrit replication deploy key registered in S5. Its presence
    is asserted as an existence gate (see main.tf) so the apply fails loudly if
    the key is missing — locking with a missing bypass actor would lock out
    replication too.
  EOT
  default     = "rebar-gerrit-replication"
}
