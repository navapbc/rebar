# S7 owns alerting, not S1's instance, volume, or backup lifecycle.
# The shared SNS endpoint comes from SSM and requires out-of-band confirmation.
# GerritReachable distinguishes a failed service on a healthy host. EC2 system
# and instance checks backstop host failure. Missing gate-probe data breaches.
# Distinct capacity and debris alarms below retain their own signals.

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
# unreachable (GerritReachable < 1, i.e. 0) for 6 consecutive 5-minute periods.
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
    host/probe stopped) for 6 consecutive 5-minute periods. Pairs with the EC2
    status-check alarms (host-down backstop) for full coverage.
  EOT

  namespace   = "Rebar/Gate"
  metric_name = "GerritReachable"
  statistic   = "Minimum"

  # PROFILE A: 6-of-6 over 30 minutes. One healthy 1 clears the alarm.
  # Head-of-script probe gaps reached 25 minutes, so shorter breaching-missing
  # windows falsely paged. EC2 status alarms still detect host death quickly.
  # This window covers a host that serves no Gerrit. See alarm-window-tuning.md.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Host-down / probe-stopped → ALARM (not INSUFFICIENT_DATA). See block comment.
  # GerritReachable is published in §1, the FIRST section of observability.sh, which makes it
  # the head-of-script liveness sentinel: it survives a run truncated by TimeoutStartSec, so it
  # reports "the timer stopped", while §5's mirror_out_of_sync reports "the run was cut short".
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

# --- Alarm 3b: EC2 CPU saturation (non-burstable host) ---------------------
# The Gerrit host is intentionally non-burstable (r7g.large), so CPUCreditBalance no longer
# exists as a meaningful signal. Watch sustained CPU saturation directly instead.
resource "aws_cloudwatch_metric_alarm" "ec2_cpu_utilization_high" {
  alarm_name        = "rebar-gerrit-cpu-utilization-high"
  alarm_description = "EC2 CPUUtilization for the non-burstable rebar Gerrit host is >= 85% for 30 minutes. This replaces burst-credit monitoring after the r7g.large resize."

  namespace   = "AWS/EC2"
  metric_name = "CPUUtilization"
  statistic   = "Average"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
  }

  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # AWS/EC2 publishes this host metric; missing data means the instance is not reporting rather
  # than CPU pressure, and the status-check alarms above own host-down detection.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9892-fff8-1f3b-4759"
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

# The /var/gerrit data-volume alarm watches git and review DB capacity, distinct
# from root pressure. observability.sh publishes mount=/var/gerrit. SNS actions
# make this managed alarm notify on both edges.
resource "aws_cloudwatch_metric_alarm" "gerrit_data_disk_high" {
  alarm_name        = "rebar-gerrit-data-disk-high"
  alarm_description = "Gerrit data volume (/var/gerrit) disk usage >= 85%. observability.sh publishes rebar/host:disk_used_percent per mount; exhaustion of this volume takes Gerrit down (git repos + review DB live here)."

  namespace   = "rebar/host"
  metric_name = "disk_used_percent"
  statistic   = "Maximum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
    mount      = "/var/gerrit"
  }

  # PROFILE A: six high or missing readings page within 30 minutes. Missing
  # data can mean an unmountable volume. One healthy value clears an ordinary
  # probe gap. The old 2-of-3 window paged on empty buckets alone.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # A stopped probe or unreadable /var/gerrit mount both suppress the percentage.
  # Neither has a truthful 0/100 placeholder, so sustained silence pages.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "c7d4"
  }
}

# This alarm sees non-site/ data-volume debris before the disk-pressure alarm.
# observability.sh 2c sums top-level entries outside site/ and lost+found with
# the same mount dimensions. The 1 GiB threshold ignores incidental files.
resource "aws_cloudwatch_metric_alarm" "gerrit_data_disk_debris" {
  alarm_name        = "rebar-gerrit-data-disk-debris"
  alarm_description = "Non-site/ content on the Gerrit data volume (/var/gerrit) exceeds 1 GiB. observability.sh publishes rebar/host:data_disk_debris_bytes; investigation/probe evidence belongs on the operator workstation, not here — see infra/runbooks/gerrit-data-volume-reclaim.md."

  namespace   = "rebar/host"
  metric_name = "data_disk_debris_bytes"
  statistic   = "Maximum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
    mount      = "/var/gerrit"
  }

  # PROFILE A (dead-man), 6-of-6 over 30 minutes — the shape of Alarm 4 above and for the same
  # reason: §2c publishes nothing when /var/gerrit is not a directory, so silence means an
  # unmountable data volume and must keep paging, while an ordinary scheduling gap must not.
  # datapoints_to_alarm == evaluation_periods delivers both — see
  # infra/runbooks/alarm-window-tuning.md and ticket a9d1-c7f3-cfd9-44ff. Debris above 1 GiB is
  # a persistent quantity, so it reads breaching on every run and pages within 30 minutes.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1073741824
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # Same reasoning as Alarm 4 and NOT the rebar/host counter convention: §2c publishes a
  # READING, not an offset delta, and it publishes NOTHING when /var/gerrit is not a
  # directory — because 0 would assert a volume we could not observe was clean. Both
  # causes of silence (a dead probe, an unmountable data volume) are worse than the
  # condition this alarm names, so silence pages.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "3e92"
  }
}
