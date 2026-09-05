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

  # PROFILE A (dead-man), 6-of-6 over 30 minutes — infra/runbooks/alarm-window-tuning.md.
  #
  # This was 2 consecutive periods (evaluation_periods = 2, datapoints_to_alarm defaulted to 2).
  # With breaching missing data that meant TWO EMPTY BUCKETS paged on their own, and ticket
  # a9d1-c7f3-cfd9-44ff measured 10.0-minute publisher inter-arrivals TWICE inside two hours —
  # enough to empty two adjacent 5-minute buckets. The publisher's contractual interval is
  # 5-9 minutes, not 5 (install-observability.sh: OnUnitActiveSec=5min is measured from the last
  # COMPLETED run, and TimeoutStartSec=240 bounds the run), so this was one straggler from a
  # false page.
  #
  # 6-of-6 requires 30 minutes with NO published 1, and datapoints_to_alarm ==
  # evaluation_periods means any single healthy datapoint clears it.
  #
  # THIS WAS 3-of-3 (15 min) UNTIL A FALSE FIRING AT 20:47Z ON 2026-09-05 FALSIFIED THE PREMISE
  # BEHIND IT. That sizing assumed §1 held the publisher's 5-9 minute cadence contract, which
  # the 8-hour sweep supported: 93% bucket presence, 10.0-minute worst gap. Then this alarm
  # entered ALARM on "no datapoints were received for 2 periods and 2 missing datapoints were
  # treated as [Breaching]" while Gerrit answered HTTPS 200 in 0.266s — and the head-of-script
  # gap was re-measured at 25.0 MINUTES, which defeats any 15-minute window. The head of the
  # script is no longer exempt from the truncation this ticket documents, so it no longer gets
  # a tighter window than the rest of Profile A.
  #
  # COST, stated plainly: detection of a genuinely unreachable Gerrit goes from 10 to 30
  # minutes. That is a real regression and it is accepted for two reasons. First, the 10-minute
  # detector demonstrably pages on healthy operation, so it was not buying 10-minute detection,
  # it was buying noise. Second, host death is still caught in ~2 minutes by the EC2
  # status-check alarms below, which are AWS-published and do not depend on this publisher at
  # all; what widens to 30 minutes is only the "host up, Gerrit not serving" case. Once
  # ignitable-fuchsia-kawala (9313-1fac-9f32-4b07) restores the publisher's cadence, this
  # should go back to 3-of-3 — see the revert conditions in
  # infra/runbooks/alarm-window-tuning.md.
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

# --- Alarm 4: Gerrit DATA-volume disk pressure (ticket c7d4) ----------------
# The /var/gerrit data volume filling is a top outage risk — git repos, All-Projects,
# and the review DB live there. observability.sh publishes rebar/host:disk_used_percent
# with a `mount` dimension per filesystem; this alarm watches mount=/var/gerrit.
#
# ADOPTS a pre-existing, UNMANAGED live alarm (rebar-gerrit-data-disk-high) that was
# created out-of-band with an EMPTY alarm_actions list (so it never notified). Bringing
# it under IaC also WIRES it to the SNS topic, closing the silent-alarm gap. Companion
# to rebar-root-disk-pressure (monitoring_autodeploy.tf), which watches the ROOT disk
# (root_disk_used_percent); this one watches the DATA volume (disk_used_percent).
#
# POST-MERGE ADOPTION: the live alarm already exists, so after this merges an operator
# imports it before apply so state matches reality (see ticket c7d4):
#   terraform import aws_cloudwatch_metric_alarm.gerrit_data_disk_high rebar-gerrit-data-disk-high
#   terraform apply   # sets alarm_actions/ok_actions on the adopted alarm
# (CloudWatch PutMetricAlarm is an idempotent upsert, so an un-imported apply would also
# adopt-by-overwrite; import is preferred so the first plan shows only the actions diff.)
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

  # PROFILE A (dead-man), 6-of-6 over 30 minutes — infra/runbooks/alarm-window-tuning.md.
  #
  # This alarm keeps "breaching" while the other level gauges move off it, because silence
  # here carries information nothing else carries: §2 publishes NOTHING when it cannot read a
  # percentage for /var/gerrit, i.e. when the data volume is gone or unmountable (see the
  # treat_missing_data comment below). That is a real condition and it must keep paging.
  #
  # What changes is the evidence rule. The 2-of-3 shape counted TWO EMPTY BUCKETS as sufficient
  # on their own, and ticket a9d1-c7f3-cfd9-44ff measured empty buckets to be guaranteed rather
  # than unlucky: the publisher's contractual inter-arrival is 5-9 minutes against 5-minute
  # buckets, and 6 of 47 buckets over four hours were empty (87% present, not the ~92% the
  # comment this replaces asserted). datapoints_to_alarm == evaluation_periods means a page now
  # needs 30 minutes with no readable percentage at all, and ANY single reading — over or under
  # 85 — decides the alarm on real evidence instead of on absence.
  #
  # Detection is unchanged in kind and slower in time: a volume genuinely at or above 85% reads
  # so on every run, so all 6 periods breach and it pages within 30 minutes.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanOrEqualToThreshold"

  # Pre-dates ticket bff5-9163-cddd-4158; that ticket only widened the window above. Missing
  # data has two causes here and BOTH warrant a page: the host-published probe stopped, or
  # observability.sh §2 read no percentage for $DATA_MOUNT (`df --output=pcent /var/gerrit`
  # produced no digits) and skipped the publish. Unlike the rebar/host counters, §2 is NOT
  # unconditional and deliberately stays that way — its metric is a reading, not a delta, so
  # there is no honest placeholder: publishing 0 would assert an empty volume and 100 would
  # fabricate a full one. A df that cannot report on the Gerrit data volume means that volume
  # is gone or unmountable, which is a worse fault than the one this alarm names, so silence
  # is allowed to page. The 6-of-6 window above absorbs an ordinary scheduling gap and a one-off
  # read failure alike, because either leaves the other five periods free to clear the alarm.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "c7d4"
  }
}

# --- Alarm 5: non-`site/` debris on the Gerrit DATA volume (task 3e92) ------
# Companion to Alarm 4 above, and deliberately a DIFFERENT question. Alarm 4 watches
# disk_used_percent — "how full is /var/gerrit" — which cannot distinguish the git repos
# and review DB the volume exists for from one-off investigation output dumped beside
# them. The 2026-08-26 fill was 65% the latter: two ~5.2G epoch-probe dumps under
# /var/gerrit/rebar-quiet-window-evidence/, invisible to every metric until a human ran
# `du` mid-incident.
#
# observability.sh §2c publishes rebar/host:data_disk_debris_bytes — the summed size of
# every top-level entry under /var/gerrit that is not `site` or `lost+found` — with the
# same InstanceId+mount dimensions as disk_used_percent. A healthy volume publishes 0, so
# this alarm fires on the PRESENCE of debris well before it becomes capacity pressure,
# which is the whole point: at 85% used the volume is already an incident.
#
# 1 GiB, not 0 bytes. A byte-exact threshold would page on a stray dotfile or an
# operator's half-second `ls > /var/gerrit/x`, and an alarm that pages on noise gets
# muted — which is how the condition went unwatched in the first place. 1 GiB is well
# under the ~11G that produced the incident and far above anything incidental.
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
