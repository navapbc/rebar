# ---------------------------------------------------------------------------
# monitoring.tf — S7 monitoring IaC: SNS alerting + gate-down / host-down alarms.
# Epic d251, story S7.
# ---------------------------------------------------------------------------
# SCOPE / SINGLE-OWNER CONTRACT: S7 only ADDS monitoring + ASSERTS invariants.
# It does NOT re-declare anything S1 owns:
#   - the EC2 instance + data volume + their prevent_destroy (main.tf)
#   - the DLM lifecycle policy retain=7 + its execution role (backup.tf / iam.tf)
# S7 reads those by DATA SOURCE and watches the resulting metrics/snapshots.
# A second declaration of any of them would put two configs in conflict over one
# resource (the exact drift class S1's single-owner comments warn against).
#
# Reuses var.aws_region and data.aws_caller_identity.current (declared in iam.tf),
# matching monitoring_s5.tf / monitoring_s4b.tf.
#
# COVERAGE MODEL — two complementary signals catch the two distinct failure modes:
#   1. gerrit_gate_down  (Rebar/Gate:GerritReachable < 1) — Gerrit is DOWN but the
#      host is up and the probe still runs: GerritReachable is published as 0.
#   2. ec2_system_check / ec2_instance_check (AWS/EC2 status checks) — the HOST
#      itself is down/unreachable: the probe stops publishing, AND AWS's own status
#      checks fail. The status-check alarms are the host-down BACKSTOP.
#   Together they distinguish "Gerrit crashed on a healthy box" from "box is gone".
#   The gate-down alarm ALSO has treat_missing_data=breaching, so a probe that stops
#   publishing (host wedged but not status-check-failing) still trips gerrit_gate_down.
# ---------------------------------------------------------------------------

# --- Alert sink: SSM-sourced email -> SNS topic + subscription -------------
# The alert email lives in SSM SecureString /rebar/prod/alert-endpoint (the slot
# is created by ssm.tf; an operator populates the real address out-of-band). Read
# it here with decryption so the subscription endpoint is not hardcoded in HCL.
data "aws_ssm_parameter" "alert_endpoint" {
  name            = "/rebar/prod/alert-endpoint"
  with_decryption = true
}

resource "aws_sns_topic" "alerts" {
  name = "rebar-gerrit-alerts"

  tags = {
    Project = "rebar"
    Story   = "S7"
  }
}

# Email subscription. NOTE: an email subscription requires a one-time, out-of-band
# CONFIRMATION click in the inbox before it delivers — terraform creates it in
# "PendingConfirmation" and AWS does not auto-confirm email. The operator must
# confirm once after the first apply.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = data.aws_ssm_parameter.alert_endpoint.value
}

# --- Alarm 1: Gerrit gate-down (the review gate's health) ------------------
# METRIC SOURCE: the host observability probe (infra/scripts/observability.sh)
# publishes Rebar/Gate:GerritReachable = 1 when the /config/server/version probe
# returned 200, else 0 — DIMENSIONLESS, to match this alarm. Fires when Gerrit is
# unreachable (GerritReachable < 1, i.e. 0) for 2 consecutive 5-minute periods.
#
# treat_missing_data = "breaching" is DELIBERATE: if the host is down or the probe
# timer has stopped, no datapoint arrives — we want that to ALARM (the gate is not
# known-healthy), not sit silently in INSUFFICIENT_DATA. This is the opposite choice
# from the count-style S5/S4b alarms (where missing = healthy steady state); here a
# present "1" is the healthy state, so absence is treated as breaching.
resource "aws_cloudwatch_metric_alarm" "gerrit_gate_down" {
  alarm_name        = "rebar-gerrit-gate-down"
  alarm_description = <<-EOT
    Gerrit (the review gate) is unreachable. The host observability probe publishes
    Rebar/Gate:GerritReachable = 1 when https://${var.dns_name}/config/server/version
    returns 200, else 0. This alarm fires when it is < 1 (i.e. 0, or MISSING — the
    host/probe stopped) for 2 consecutive 5-minute periods. Pairs with the EC2
    status-check alarms (host-down backstop) for full coverage.
  EOT

  namespace   = "Rebar/Gate"
  metric_name = "GerritReachable"
  statistic   = "Minimum"

  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Host-down / probe-stopped → ALARM (not INSUFFICIENT_DATA). See block comment.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "S7"
  }
}

# --- The Gerrit host (read by tag, NOT hardcoded) --------------------------
# Resolve the running instance by its Name tag so the status-check alarms bind to
# the live instance id without hardcoding i-00880b2c7f13527c5. S1 owns the instance
# (main.tf, tag Name=rebar-gerrit); S7 only reads it.
data "aws_instance" "gerrit" {
  filter {
    name   = "tag:Name"
    values = ["rebar-gerrit"]
  }
  # Exclude a terminated instance lingering in the API from matching.
  filter {
    name   = "instance-state-name"
    values = ["pending", "running", "stopping", "stopped"]
  }
}

# --- Alarm 2 + 3: EC2 status checks (host-down backstop) -------------------
# AWS/EC2 status checks are native (no probe needed). System check = the AWS
# infrastructure underneath the instance; Instance check = the instance's own OS
# reachability. Either failing for 2 consecutive 1-minute periods means the box is
# unhealthy — the backstop for "the probe can't publish because the host is gone".
resource "aws_cloudwatch_metric_alarm" "ec2_system_check" {
  alarm_name        = "rebar-gerrit-ec2-system-check"
  alarm_description = "EC2 system status check failed for the rebar Gerrit host (underlying AWS infrastructure). Host-down backstop alongside gerrit_gate_down."

  namespace   = "AWS/EC2"
  metric_name = "StatusCheckFailed_System"
  statistic   = "Maximum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
  }

  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "S7"
  }
}

resource "aws_cloudwatch_metric_alarm" "ec2_instance_check" {
  alarm_name        = "rebar-gerrit-ec2-instance-check"
  alarm_description = "EC2 instance status check failed for the rebar Gerrit host (instance OS reachability). Host-down backstop alongside gerrit_gate_down."

  namespace   = "AWS/EC2"
  metric_name = "StatusCheckFailed_Instance"
  statistic   = "Maximum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
  }

  period              = 60
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Story   = "S7"
  }
}

# --- ASSERT (not own): the DLM-targeted data volume exists -----------------
# S1 OWNS the DLM daily-snapshot policy (backup.tf, retain=7) and the data volume
# with prevent_destroy (main.tf). S7 declares NO aws_dlm_lifecycle_policy. It only
# reads the data volume (the DLM snapshot target, tag Name=rebar-gerrit-data) and
# ASSERTS via a `check` block that the monitored backup target is present — so a
# drift that removed the volume surfaces as a check warning on `plan`/`apply`,
# without S7 ever managing the resource.
data "aws_ebs_volume" "data" {
  most_recent = true

  filter {
    name   = "tag:Name"
    values = ["rebar-gerrit-data"]
  }
}

check "backup_target_present" {
  assert {
    condition     = data.aws_ebs_volume.data.id != ""
    error_message = "The DLM snapshot target volume (tag Name=rebar-gerrit-data) was not found. S1's data volume + DLM retain=7 policy is the backup of record; S7 only monitors it. Investigate before relying on the restore drill."
  }
}

# Discoverability anchor: account/region provenance, matching iam.tf usage and the
# locals in monitoring_s5.tf / monitoring_s4b.tf.
locals {
  monitoring_region     = var.aws_region
  monitoring_account_id = data.aws_caller_identity.current.account_id
}
