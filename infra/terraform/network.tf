# ---------------------------------------------------------------------------
# Network — Default VPC + security group
# ---------------------------------------------------------------------------
# DELIBERATE PoC SIMPLIFICATION: we use the account's Default VPC. All of its
# subnets are public (each has a route to the internet gateway), so there is no
# private-subnet / NAT tier here. The SECURITY GROUP is the security boundary —
# it is what restricts inbound access, not network topology. A production build
# would introduce private subnets + a load balancer; that is out of scope here.
# ---------------------------------------------------------------------------

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

resource "aws_security_group" "gerrit" {
  name        = "rebar-gerrit-sg"
  description = "Inbound HTTPS + Gerrit SSH for the rebar Gerrit host. No port 22 - admin is via SSM Session Manager."
  vpc_id      = data.aws_vpc.default.id

  # HTTPS (Gerrit web UI / REST).
  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # Gerrit SSH (git over SSH on Gerrit's dedicated port — NOT system sshd).
  ingress {
    description = "Gerrit SSH"
    from_port   = 29418
    to_port     = 29418
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  # NOTE: NO inbound 22/tcp. Administrative shell access is exclusively via AWS
  # SSM Session Manager (the instance role grants AmazonSSMManagedInstanceCore),
  # so the box has no exposed OpenSSH port to attack.

  egress {
    description = "All outbound"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name    = "rebar-gerrit-sg"
    Project = "rebar"
  }
}
