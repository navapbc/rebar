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
  default     = 30
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
