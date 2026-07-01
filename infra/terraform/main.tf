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
  user_data = templatefile("${path.module}/user_data.sh", {
    data_volume_id = aws_ebs_volume.data.id
  })

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
