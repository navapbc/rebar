variable "aws_region" {
  type        = string
  description = "AWS region for all resources."
  default     = "us-east-1"
}

variable "instance_type" {
  type        = string
  description = "EC2 instance type. t4g.large is Graviton/arm64 — must match the arm64 AL2023 AMI."
  default     = "t4g.large"
}

variable "root_volume_size_gb" {
  type        = number
  description = "Size of the EC2 root EBS volume, in GiB."
  default     = 60
}

variable "data_volume_size_gb" {
  type        = number
  description = "Size of the dedicated Gerrit data EBS volume, in GiB."
  default     = 50
}

variable "snapshot_retention_count" {
  type        = number
  description = "Number of daily DLM snapshots of the data volume to retain."
  default     = 7
}

variable "dns_zone_id" {
  type        = string
  description = "Route53 public hosted zone id for solutions.navateam.com."
  default     = "Z05558453EZPQLHKC20IQ"
}

variable "dns_name" {
  type        = string
  description = "Fully-qualified DNS name to point at the Gerrit Elastic IP."
  default     = "rebar.solutions.navateam.com"
}

# --- Gate-scratch volume (ADR 0112 decision 3, story aa40-cbda-ee38-481c) ----
# Review-gate snapshots and the review-bot's per-review clones used to share the OS/root
# filesystem, so a review burst could wedge the whole host (bug 3276, a 5h outage). These
# two variables size and place the dedicated volume that carries them instead.

variable "gate_scratch_volume_size_gb" {
  type        = number
  description = <<-EOT
    Size of the dedicated review-gate scratch EBS volume, in GiB.

    50 GiB is a MEASURED default, not a round number: ADR 0112 recorded gate/investigation
    scratch at 3.6G of the 28G root working set at the outage, and story 09da bounds
    concurrent gate runs at 4, each holding one content-addressed snapshot entry plus one
    per-review clone. 50 GiB leaves ~42 GiB below the 85% alarm — roughly an order of
    magnitude over observed peak. It is a variable rather than a constant per ADR 0112:
    the knob an operator needs mid-incident must not require a code change.
  EOT
  default     = 50
}

variable "gate_scratch_mount" {
  type        = string
  description = <<-EOT
    Host path the gate-scratch volume mounts at. Single-sourced here because four things
    must agree on it — user_data.sh's fstab entry, the review-bot's REBAR_GATE_TMPDIR/TMPDIR,
    observability.sh's df probe, and the CloudWatch alarm's `mount` dimension. A disagreement
    between any two of them is a silent fallback to the root filesystem.
  EOT
  default     = "/var/lib/rebar/gate-scratch"
}
