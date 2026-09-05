# ---------------------------------------------------------------------------
# main.tf — Gerrit EC2 instance, data volume, Elastic IP
# ---------------------------------------------------------------------------

provider "aws" {
  region = var.aws_region
}

# AL2023 arm64 AMI resolved from the public SSM parameter (NOT a hardcoded id),
# so we always launch the current patched image. `insecure_value` is correct
# here: this is a public, non-secret SSM parameter holding an AMI id.
data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# The instance's subnet — looked up so both the instance and the data volume can
# derive their AZ from the SAME source. (Deriving the volume's AZ from the
# instance directly creates an apply cycle: instance.user_data needs the volume
# id, and the volume would need the instance's AZ.)
data "aws_subnet" "selected" {
  id = data.aws_subnets.default.ids[0]
}

# Dedicated data volume for Gerrit's site (repos, indexes, config). Kept as a
# SEPARATE EBS volume (not the root) with prevent_destroy so a `terraform
# destroy` / instance replacement never silently takes the Gerrit data with it.
resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.selected.availability_zone
  size              = var.data_volume_size_gb
  type              = "gp3"

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name    = "rebar-gerrit-data"
    Project = "rebar"
  }
}

# Dedicated scratch volume for review-gate work: the content-addressed snapshot
# store and the review-bot's per-review `reviewbot-*` clones (ADR 0112 decision 3,
# story aa40-cbda-ee38-481c). Both used to live on the OS/root filesystem, so a
# review burst could fill root and fail-close Gerrit, the LLM-Review votes and the
# MCP server together (bug 3276-2f81-8c75-4ddd, a five-hour outage).
#
# DELIBERATELY NOT prevent_destroy, unlike aws_ebs_volume.data above. This volume is
# REBUILDABLE: every byte on it is either a snapshot that re-materialises from a git
# ref or a working clone that re-clones. Nothing here is source-of-truth data, so
# protecting it would only make an ordinary teardown need a manual override for a
# volume no one needs to keep. The Name tag says so too, for whoever reads the
# console rather than this file.
resource "aws_ebs_volume" "gate_scratch" {
  availability_zone = data.aws_subnet.selected.availability_zone
  size              = var.gate_scratch_volume_size_gb
  type              = "gp3"

  tags = {
    Name    = "rebar-gate-scratch"
    Project = "rebar"
    Data    = "rebuildable-scratch"
    Ticket  = "aa40"
  }
}

resource "aws_instance" "gerrit" {
  ami                  = data.aws_ssm_parameter.al2023_ami.insecure_value
  instance_type        = var.instance_type
  subnet_id            = data.aws_subnets.default.ids[0]
  iam_instance_profile = aws_iam_instance_profile.gerrit_instance.name

  vpc_security_group_ids = [aws_security_group.gerrit.id]

  # IMDSv2 required (token-backed metadata; defends against SSRF credential theft).
  metadata_options {
    http_tokens   = "required"
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = var.root_volume_size_gb
  }

  # user_data resolves the data volume's NVMe device dynamically by volume id;
  # we pass the id in so the script doesn't have to guess /dev/sdf vs /dev/nvme*.
  #
  # GZIPPED, and that is load-bearing (bug a68c-9633-248c-4b06). EC2 caps UserData at
  # 16,384 bytes and the provider validates it, so the plain `user_data` form stopped
  # being PLANNABLE at all once the rendered script reached 16,668 bytes -- terraform
  # could not generate a plan for ANY resource in this configuration, which blocked
  # every apply for a day. cloud-init detects the gzip magic and decompresses before
  # interpreting, so the script it runs is byte-identical to the rendered template.
  #
  # The script is ~73% comments, which is deliberate: templatefile() interpolates the
  # WHOLE file including comments (bug dd30), so that prose is the context an editor
  # needs to avoid re-breaking it. Compression is what lets the documentation and the
  # size limit coexist -- 16,668 bytes render to 6,954 gzipped, a 2.4x margin. Do NOT
  # "fix" a future overflow by deleting comments; scripts/check_user_data_size.py
  # measures the payload AWS actually receives and will say how much room is left.
  #
  # `user_data_replace_on_change` is left at its default (false), so a change here is
  # an IN-PLACE attribute update that takes effect on the instance's next boot. It
  # must never force-replace aws_instance.gerrit: that is the production Gerrit host.
  user_data_base64 = base64gzip(templatefile("${path.module}/user_data.sh", {
    data_volume_id         = aws_ebs_volume.data.id
    gate_scratch_volume_id = aws_ebs_volume.gate_scratch.id
    gate_scratch_mount     = var.gate_scratch_mount
  }))

  # Pin the AMI: a new SSM-published AMI id must NOT force-replace the running
  # instance on every apply. Replacement is an explicit, deliberate action.
  lifecycle {
    ignore_changes = [ami]
  }

  tags = {
    Name    = "rebar-gerrit"
    Project = "rebar"
  }
}

# Attach the data volume. We request /dev/sdf, but on Nitro/Graviton the kernel
# surfaces EBS as an NVMe device (/dev/nvme*n1) — which is exactly why
# user_data.sh resolves the device dynamically by volume id rather than trusting
# this path.
resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.gerrit.id
}

# Attach the scratch volume. Same Nitro caveat as the data volume: /dev/sdg is what we
# request, not what the kernel presents, so user_data.sh resolves it by volume id.
resource "aws_volume_attachment" "gate_scratch" {
  device_name = "/dev/sdg"
  volume_id   = aws_ebs_volume.gate_scratch.id
  instance_id = aws_instance.gerrit.id
}

# Stable public address (survives instance stop/start and replacement).
resource "aws_eip" "gerrit" {
  instance = aws_instance.gerrit.id
  domain   = "vpc"

  lifecycle {
    prevent_destroy = true
  }

  tags = {
    Name    = "rebar-gerrit-eip"
    Project = "rebar"
  }
}

output "instance_id" {
  description = "EC2 instance id of the Gerrit host."
  value       = aws_instance.gerrit.id
}

output "public_ip" {
  description = "Elastic IP associated with the Gerrit host."
  value       = aws_eip.gerrit.public_ip
}

output "data_volume_id" {
  description = "EBS volume id of the Gerrit data volume."
  value       = aws_ebs_volume.data.id
}

output "gate_scratch_volume_id" {
  description = "EBS volume id of the review-gate scratch volume (rebuildable, not backed up)."
  value       = aws_ebs_volume.gate_scratch.id
}
