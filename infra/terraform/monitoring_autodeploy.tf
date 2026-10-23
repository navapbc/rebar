# The host probe counts AUTODEPLOY_ERROR journal markers as dimensionless
# rebar/host:deploy_errors and alerts via shared SNS. Sustained failures mean
# the box is not tracking main. The last healthy review-bot remains serving.
# Metric namespace/name/dimensions must match the probe exactly.

resource "aws_cloudwatch_metric_alarm" "deploy_errors" {
  alarm_name        = "rebar-autodeploy-errors"
  alarm_description = <<-EOT
    rebar continuous auto-deploy failures detected in the rebar-autodeploy.service journal
    (AUTODEPLOY_ERROR markers: fetch / config-check / materialise / review-bot build or
    health-check-then-rollback). Published as rebar/host:deploy_errors by the host
    observability probe (§4d). The deploy is fail-safe (last-known-good stays live, gate not
    frozen), but a sustained signal means `main` is not reaching the box — investigate
    `journalctl -u rebar-autodeploy` and the target main tip. Disable via
    `systemctl disable --now rebar-autodeploy.timer`; the manual deploy path still works.
  EOT

  namespace   = "rebar/host"
  metric_name = "deploy_errors"
  statistic   = "Sum"

  # Cadence MUST match the deploy's capped backoff (autodeploy.sh BACKOFF_CAP=900s):
  # once backed off, failures arrive at most once per 15 min, so two CONSECUTIVE
  # 5-minute periods > 0 essentially never happen — the original 300s/2-consecutive
  # shape stayed silent through 41h of continuous deploy failure (incident 2731,
  # bug ac14). 15-minute periods with 2-of-4 datapoints latch a persistent failure
  # loop within ~an hour while a single transient error still doesn't page.
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B: observed errors page. Missing data alone does not.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching". The configured window catches observed
  # intermittent errors without false pages when the probe misses a zero.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Epic    = "88ab"
    Story   = "8903"
  }
}

# A deploy can cancel an in-flight review without a voter error or bot restart.
# The change then stalls at Verified +1 and LLM-Review 0. Defer deploys while
# reviews run. If a deploy proceeds anyway, distinguish an exhausted
# DEPLOY_DEFER_MAX bound from unreadable /health in_flight. They need different
# responses. Healthy deploy_deferrals do not page.
# The host probe publishes dimensionless rebar/host Count metrics named
# review_interrupts_bound_exceeded and review_interrupts_signal_unavailable.
# The rolled-up review_interrupts total remains published but has no alarm.
# Both alarms use 900s, 2-of-4, Sum > 0. Healthy zero and missing-data policy
# are explained at each alarm. Keep dimensions absent on both publisher and alarm.

resource "aws_cloudwatch_metric_alarm" "review_interrupts_bound_exceeded" {
  alarm_name        = "rebar-autodeploy-review-interrupts-bound-exceeded"
  alarm_description = <<-EOT
    An auto-deploy recreated the review-bot mid-review, killing an LLM-Review: the
    DEPLOY_DEFER_MAX deferral bound was exhausted with reviews STILL in flight and the deploy
    proceeded anyway (AUTODEPLOY_REVIEW_INTERRUPT, reason `bound-exceeded`, in the
    rebar-autodeploy.service journal; published as rebar/host:review_interrupts_bound_exceeded
    by the host probe, observability.sh 4e). MEANING: the drain check is WORKING — the bot is
    chronically busy and a deploy ran out of patience. The backfill reconciler retries killed
    reviews, so a lone firing in a landing burst is not an outage. REMEDIATION: repeated
    firings are a review-THROUGHPUT problem, not a probe problem — check review duration and
    concurrency, and whether DEPLOY_DEFER_MAX is sized for them; left alone they live-lock the
    gate (changes reach `Verified +1` with `LLM-Review = 0` and cannot submit). If
    rebar-autodeploy-review-interrupts-signal-unavailable is also firing, that one is primary.
  EOT

  namespace   = "rebar/host"
  metric_name = "review_interrupts_bound_exceeded"
  statistic   = "Sum"

  # Cadence matches the deferral bound (autodeploy.sh DEPLOY_DEFER_MAX=2400s): a bound-exceeded
  # interrupt cannot recur faster than one per episode. The pre-bff5 shape latched on a SINGLE
  # 900s period; with treat_missing_data = "breaching" below, a 1-datapoint latch would also page
  # on one absent period, so the window is 2 breaching datapoints out of 4. A real interrupt loop
  # recurs on the next deploy and a genuinely dead probe stays absent, so either reaches the
  # second datapoint — the cost is detection at ~30 min instead of ~15.
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B: observed errors page. Missing data alone does not.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching". The configured window catches observed
  # intermittent errors without false pages when the probe misses a zero.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "34cd"
  }
}

resource "aws_cloudwatch_metric_alarm" "review_interrupts_signal_unavailable" {
  alarm_name        = "rebar-autodeploy-review-interrupts-signal-unavailable"
  alarm_description = <<-EOT
    An auto-deploy could not read the review-bot's /health `in_flight` signal and recreated the
    container with NO drain check at all (AUTODEPLOY_REVIEW_INTERRUPT, reason
    `signal-unavailable`, in the rebar-autodeploy.service journal; published as
    rebar/host:review_interrupts_signal_unavailable by the host probe, observability.sh 4e).
    MEANING: this is the URGENT one. The drain check itself is broken, so EVERY deploy is now
    running blind and killing whatever review is in flight — the exact failure mode bug 34cd
    exists to catch, silently regressed. It leaves voter_errors, restarts and the deploy log all
    green, so no other alarm can see it. REMEDIATION: confirm the review-bot is up and its
    /health is reachable FROM THE HOST and still returns an `in_flight` field — autodeploy.sh
    bot_in_flight_reviews echoes -1 for unreachable/missing/unparseable and that fail-open path
    is what fires this. It recurs on every deploy while broken, hence the tight 2-of-4 window.
  EOT

  namespace   = "rebar/host"
  metric_name = "review_interrupts_signal_unavailable"
  statistic   = "Sum"

  # This case must surface fast, hence the tight 2-of-4 window rather than a slower one. It
  # cannot be the pre-bff5 1-of-1 latch any more: with treat_missing_data = "breaching" below a
  # single absent period would page, and absent periods are ordinary timer jitter. The failure
  # this alarm watches recurs on EVERY deploy while broken, so it reaches 2 datapoints quickly.
  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # PROFILE B: observed errors page. Missing data alone does not.
  # rebar:allow-missing-data-notbreaching: liveness is carried by the dead-man alarms, which keep
  # treat_missing_data = "breaching". The configured window catches observed
  # intermittent errors without false pages when the probe misses a zero.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "34cd"
  }
}

# mechanism-ok: ci_gate rebar-reviewbot-webhook-auth-rejections — 073f-b86f-42c6-456f: pages when Gerrit delivers webhooks with a stale token while the bot process and outbound Gerrit auth still look healthy.
resource "aws_cloudwatch_metric_alarm" "reviewbot_webhook_auth_rejections" {
  alarm_name        = "rebar-reviewbot-webhook-auth-rejections"
  alarm_description = <<-EOT
    The review-bot rejected Gerrit webhook deliveries with a missing or stale X-Rebar-Token.
    This is the rotation failure mode where /review/health can stay HTTP 200 and Gerrit auth
    can stay OK while no patchset-created/comment-added webhook is accepted, so LLM-Review votes
    stall silently. Published as rebar/host:reviewbot_webhook_auth_rejections by
    observability.sh from the review-bot health payload. Remediation: check
    infra/gerrit/service-user.sh rotation output, refs/meta/config:webhooks.config, and restart
    or reload Gerrit/webhooks so the plugin uses the current token.
  EOT

  namespace   = "rebar/host"
  metric_name = "reviewbot_webhook_auth_rejections"
  statistic   = "Sum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
  }

  period              = 900
  evaluation_periods  = 4
  datapoints_to_alarm = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"

  # rebar:allow-missing-data-notbreaching: liveness is carried by the reviewbot_healthy and
  # host dead-man alarms; this counter must page on observed rejected webhooks, not on a missing
  # probe run.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "073f-b86f-42c6-456f"
  }
}

# observability.sh 2 publishes dimensionless rebar/host:root_disk_used_percent
# from `df` on /. Root exhaustion breaks review-bot clone/build work. This is
# separate from the /var/gerrit data-volume alarm.

resource "aws_cloudwatch_metric_alarm" "root_disk_pressure" {
  alarm_name        = "rebar-root-disk-pressure"
  alarm_description = <<-EOT
    The rebar box's 60G ROOT filesystem is above 85% used. This is the BACKSTOP
    (ADR 0112 decision 2): it says "root disk high" and cannot name which of the four
    accumulators grew, so read the per-generator alarms FIRST —
    rebar-docker-storage-cap-high, rebar-docker-buildkit-cache-high and
    rebar-docker-unaccounted-bytes — before reaching for `du`. Gate scratch has its own
    volume and its own alarms. Published as rebar/host:root_disk_used_percent by
    observability.sh §2b (5-min cadence).
  EOT

  namespace   = "rebar/host"
  metric_name = "root_disk_used_percent"
  statistic   = "Maximum"

  # PROFILE A: six 5-minute breaching or empty periods page. The probe's 5-9 minute
  # cadence leaves empty buckets. A 2-of-3 breaching window falsely paged and could
  # stay in ALARM despite healthy readings. With 6-of-6, one healthy reading clears
  # the alarm. Sustained disk pressure remains visible within 30 minutes. See
  # infra/runbooks/alarm-window-tuning.md.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # Missing data means the host-published disk probe stopped; page rather than
  # letting a dying host clear its own disk-pressure alarm to OK.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Bug     = "ac14"
  }
}

# Gate snapshots and reviewbot clones use a separate scratch volume. Alarm on
# both capacity and mount presence. An unmounted volume can read as 0% used
# while all gate runs refuse.

resource "aws_cloudwatch_metric_alarm" "gate_scratch_disk_high" {
  alarm_name        = "rebar-gate-scratch-disk-high"
  alarm_description = <<-EOT
    The dedicated review-gate scratch volume is above 85% used. Gate snapshots
    (rebar-gate-snapshots) and the review-bot's per-review reviewbot-* clones live
    here; exhaustion refuses gates instead of taking the OS disk with it (ADR 0112
    decision 3). Reclaim runs through the snapshot janitor; the contents are
    REBUILDABLE, so a stuck volume may be cleared wholesale. Published as
    rebar/host:disk_used_percent with mount=/var/lib/rebar/gate-scratch by
    observability.sh 2e (5-min cadence).
  EOT

  namespace   = "rebar/host"
  metric_name = "disk_used_percent"
  statistic   = "Maximum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
    mount      = var.gate_scratch_mount
  }

  # Six breaching or empty five-minute periods page. One healthy reading
  # clears ordinary probe gaps. A missing mount has its own heartbeat below.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # Bug 3276's defect 2 in one line: an alarm whose metric stops must PAGE, not clear
  # itself to OK. Pinned by tests/unit/test_alarm_actions_terraform.py so this is not
  # a copy-paste that a later edit can quietly drop.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "aa40"
  }
}

resource "aws_cloudwatch_metric_alarm" "gate_scratch_unmounted" {
  alarm_name        = "rebar-gate-scratch-unmounted"
  alarm_description = <<-EOT
    The review-gate scratch volume is NOT mounted at its expected path. rebar's gate
    admission refuses every plan-review and completion-verifier run in this state
    (GateScratchUnavailableError) rather than repopulating the snapshot store on the
    ROOT filesystem, so the visible symptom is "all gates refuse", not disk pressure.
    Remount the volume (see infra/runbooks/review-bot-ops.md). Published as
    rebar/host:gate_scratch_mounted, a 1/0 heartbeat on every probe tick.
  EOT

  namespace   = "rebar/host"
  metric_name = "gate_scratch_mounted"
  statistic   = "Minimum"

  # Section 2e publishes 1 mounted or 0 unmounted on every tick. Six
  # unhealthy or absent periods page. One published 1 clears a probe gap.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics: the healthy path publishes 1 on EVERY tick, so absence means
  # the probe, the timer, or the host is dead — the one state this alarm most needs to
  # announce.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "aa40"
  }
}

# mechanism-ok: ci_gate rebar-gate-scratch-volume-not-in-service — eb65-84c3-071a-404b:
# pages when Terraform's attached scratch volume is not the filesystem mounted at the gate
# scratch path, closing the delivered-but-not-in-effect gap left by marker-only monitoring.
resource "aws_cloudwatch_metric_alarm" "gate_scratch_volume_not_in_service" {
  alarm_name        = "rebar-gate-scratch-volume-not-in-service"
  alarm_description = <<-EOT
    Terraform's dedicated review-gate scratch EBS volume is not confirmed as the
    filesystem mounted at the gate scratch path. This catches the applied-but-not-in-use
    state: a marker may exist, and df may publish, while the intended EBS volume is not the
    one backing /var/lib/rebar/gate-scratch. Published as
    rebar/host:gate_scratch_volume_in_service with InstanceId, VolumeId, and mount
    dimensions by observability.sh every probe tick. Values: 1 in service, 0 mounted
    elsewhere/not mounted, -1 unknown/check failure.
  EOT

  namespace   = "rebar/host"
  metric_name = "gate_scratch_volume_in_service"
  statistic   = "Minimum"

  dimensions = {
    InstanceId = data.aws_instance.gerrit.id
    VolumeId   = aws_ebs_volume.gate_scratch.id
    mount      = var.gate_scratch_mount
  }

  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics: the healthy path publishes 1 on EVERY tick, while 0 and -1 both mean
  # the live volume-to-mount join is either bad or unmeasured.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "eb65"
  }
}

# Docker alarms distinguish total budget, BuildKit's internal share, and bytes
# omitted by `docker system df`. Unaccounted bytes need daemon-level diagnosis.
# Pruning cannot reclaim storage Docker does not track. Measure the whole Docker
# root, not a specific engine layer path. observability.sh 2f publishes these
# dimensionless metrics. docker-storage-cap.sh owns both percent denominators
# and the daemon's BuildKit policy.

resource "aws_cloudwatch_metric_alarm" "docker_storage_cap_high" {
  alarm_name        = "rebar-docker-storage-cap-high"
  alarm_description = <<-EOT
    /var/lib/docker is above 85% of its configured budget (ADR 0112 decision 1; the budget and
    its BuildKit/image split live in infra/scripts/docker-storage-cap.sh). This is the whole
    Docker accumulator, measured from the FILESYSTEM (`du`), not from `docker system df` —
    so it rises with bytes Docker's own accounting cannot see. Check
    rebar-docker-buildkit-cache-high and rebar-docker-unaccounted-bytes FIRST: they name
    which generator grew, and the second one decides whether pruning can help at all. Never
    delete under /var/lib/docker by hand. Published as rebar/host:docker_storage_used_percent
    by observability.sh 2f (5-min cadence). Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "docker_storage_used_percent"
  statistic   = "Maximum"

  # Three measured high readings within six periods identify capacity pressure. Missing
  # measurements are not capacity evidence. docker_du_not_ok pages on failed du.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 3
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # rebar:allow-missing-data-notbreaching: docker_du_not_ok owns the bounded root-du failure
  # state, so this alarm stays about measured capacity rather than paging on "could not check".
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9183"
  }
}

resource "aws_cloudwatch_metric_alarm" "docker_du_not_ok" {
  alarm_name        = "rebar-docker-du-not-ok"
  alarm_description = <<-EOT
    The Docker root `du` census could not complete for a full 30-minute window. This is the
    accepted degradation from bug 5993: the storage and unaccounted-byte gauges intentionally
    go stale instead of publishing a plausible zero, and this heartbeat names that condition.
    Published as rebar/host:docker_du_ok (1 success / 0 failed) by observability.sh 2f.
  EOT

  namespace   = "rebar/host"
  metric_name = "docker_du_ok"
  statistic   = "Minimum"

  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics: a healthy measurement publishes 1 on every tick, a bounded
  # non-measurement publishes 0, and absence means the probe/timer/host stopped.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "5993"
  }
}

resource "aws_cloudwatch_metric_alarm" "docker_buildkit_cache_high" {
  alarm_name        = "rebar-docker-buildkit-cache-high"
  alarm_description = <<-EOT
    BuildKit's cache is above 85% of ITS OWN share of the Docker budget. A GENERATOR alarm: it
    fires while the volume is still comfortable - at 85% of the root disk the box is already
    an incident. The cache is capped by the daemon's builder.gc policy
    (/etc/docker/daemon.json, from infra/scripts/docker-storage-cap.sh), so a cache above its
    cap means that policy is not in force. LOADED is not EFFECTIVE: the right number in
    daemon.json only proves it was written, a later daemon start only that it was read.
    BuildKit SILENTLY ignores a space key it does not recognise, so both can hold while the
    enforced ceiling is BuildKit's default. That key is a BUILDKIT feature, not an Engine one
    (maxUsedSpace at BuildKit >= 0.13, defaultKeepStorage below), and the Engine version does
    not imply the BuildKit version - bug 3057: Engine 25.0.16, BuildKit 0.12.6. Re-run
    docker-storage-cap.sh --install; it reports effective keep-bytes and warns on mismatch.
    Metric: rebar/host:docker_buildkit_cache_used_percent (observability.sh 2f).
  EOT

  namespace   = "rebar/host"
  metric_name = "docker_buildkit_cache_used_percent"
  statistic   = "Maximum"

  # Six high or absent five-minute periods page. A healthy cache reading
  # clears ordinary probe gaps. Section 2f publishes after a successful measure.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # Silence here means `docker system df` did not answer — a wedged or dead daemon, which is
  # never the healthy reading it would otherwise be mistaken for.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9183"
  }
}

resource "aws_cloudwatch_metric_alarm" "docker_unaccounted_bytes" {
  alarm_name        = "rebar-docker-unaccounted-bytes"
  alarm_description = <<-EOT
    More than 2 GiB of apparent-size content exists under /var/lib/docker that `docker system
    df` does NOT account for in ANY of its rows. MEANING: these bytes are unreachable by
    `docker prune` — the daemon does not know they are there. At the 2026-09-02 outage this was
    ~6.5 GB of orphaned overlay2 and four prune rounds recovered ~1.06 GB against a 29 GB
    problem, so "prune harder" is WRONG. DIAGNOSIS: the rebar-health log line carries
    allocated root, apparent root, overlay2 and ledger readings, separating allocation overhead
    from a named subtree. REMEDIATION: stop the build path, then a daemon-level reclaim (`docker system
    prune -a` with the serving containers up, or a daemon restart scheduled per the runbook).
    NEVER rm anything under /var/lib/docker: the layer metadata is the daemon's, and deleting
    behind its back desynchronises it from the tree. Published as
    rebar/host:docker_unaccounted_bytes by observability.sh 2f.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "docker_unaccounted_bytes"
  statistic   = "Maximum"

  # Three measured high readings within six periods identify unaccounted bytes. Missing
  # measurements are not residue evidence. docker_du_not_ok pages on failed du.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 3
  # 2 GiB. This is deliberately not "any divergence": the production baseline on 2026-09-05 was
  # 13,642,739,712 allocated bytes, 12,140,814,875 apparent bytes, and a 10,957,600,000-byte
  # Docker ledger, i.e. 1,183,214,875 apparent bytes not in the ledger. The threshold is above
  # that baseline while remaining far below the ~6.5 GB that went unnoticed.
  threshold           = 2147483648
  comparison_operator = "GreaterThanThreshold"

  # rebar:allow-missing-data-notbreaching: docker_du_not_ok owns bounded root-du staleness; a
  # missing residue datapoint must not be reported as orphaned bytes above threshold.
  treat_missing_data = "notBreaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "9183"
  }
}

# Journal usage and cap-in-effect are separate alarms. A config file can name a
# ceiling that the running journald has not read. SystemMaxUse only covers
# /var/log/journal and can exceed its ceiling by one active file. Root pressure
# covers the rest of /var/log. observability.sh 2g publishes dimensionless
# metrics. journald-cap.sh owns the cap and rendered configuration.

resource "aws_cloudwatch_metric_alarm" "journal_usage_high" {
  alarm_name        = "rebar-journal-usage-high"
  alarm_description = <<-EOT
    The systemd journal is above 85% of its configured ceiling (SystemMaxUse; the value lives in
    infra/scripts/journald-cap.sh, readable with `--print-env`). journald will vacuum ARCHIVED
    journal files to stay under the ceiling, so this is a warning that history is about to be
    discarded rather than that the disk is about to fill — but sustained growth here is a
    generator to investigate, because the ceiling is not the whole story: only archived files
    are vacuumed, so usage can exceed it by up to one active file. CHECK
    rebar-journal-cap-not-in-effect FIRST: if that alarm is also firing, this percentage is
    measured against a ceiling the running journald never read. DIAGNOSIS: `journalctl
    --disk-usage`, `du -sx /var/log/journal`. REMEDIATION: `journalctl --vacuum-size=` (never
    `rm` under /var/log/journal). Published as rebar/host:journal_used_percent by
    observability.sh 2g. Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "journal_used_percent"
  statistic   = "Maximum"

  # Six high or absent five-minute periods page. One healthy reading
  # clears a gap. Section 2g publishes after a successful journal measure.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # observability.sh 2g publishes this ONLY on a successful measurement, so absence means the
  # probe could not size the journal — which is exactly when this must page rather than clear to
  # OK (bug 3276 defect 2). Pinned by tests/unit/test_alarm_actions_terraform.py.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "e956"
  }
}

resource "aws_cloudwatch_metric_alarm" "journal_cap_not_in_effect" {
  alarm_name        = "rebar-journal-cap-not-in-effect"
  alarm_description = <<-EOT
    The journal ceiling on disk is NOT the one the running systemd-journald is enforcing.
    journald reads its configuration at STARTUP only and the unit implements no ExecReload, so a
    drop-in installed under a live daemon stays dormant until that daemon restarts. While this
    fires, rebar-journal-usage-high is computed against a denominator that is not in force and
    can read healthy while the journal grows to whatever ceiling the daemon read at boot (or to
    journald's derived default). CONFIRM with `bash infra/scripts/journald-cap.sh
    --check-active` (prints 1/0) and `--install`, which reports the state in words. REMEDIATE by
    restarting the logger: `systemctl restart systemd-journald`. Published as
    rebar/host:journal_cap_in_effect, a 1/0 heartbeat on every probe tick.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "journal_cap_in_effect"
  statistic   = "Minimum"

  # Section 2g publishes this 1/0 cap heartbeat each tick. Six missing
  # or 0 readings page. One healthy 1 clears ordinary scheduling gaps.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics (the gate_scratch_unmounted shape): the healthy path publishes 1 on
  # EVERY tick, so absence means the probe, the timer, or the host is dead — the one state this
  # alarm most needs to announce.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "e956"
  }
}

# observability.sh 2h publishes dimensionless /var/tmp usage and cleanup
# metrics. vartmp-cap.sh owns the budget and reaper. Publish but do not alarm
# on var_tmp_hard_quota_in_effect until rootflags=pquota is enabled by reboot.

resource "aws_cloudwatch_metric_alarm" "var_tmp_usage_high" {
  alarm_name        = "rebar-var-tmp-usage-high"
  alarm_description = <<-EOT
    /var/tmp is above 85% of its configured byte budget (the value lives in
    infra/scripts/vartmp-cap.sh, readable with `--print-env`). READ THIS DIFFERENTLY FROM THE
    OTHER GENERATOR ALARMS: unless rebar/host:var_tmp_hard_quota_in_effect is 1, nothing
    ENFORCES this number — /var/tmp is a directory on the root XFS filesystem with no
    writer-side cap, and the budget is held by rebar-var-tmp-reaper.timer, which evicts
    oldest-first every 5 minutes and can be outrun by a fast writer. So this can be the
    early warning it looks like, or it can already be a root-volume incident in progress; check
    rebar-root-disk-pressure alongside it. DIAGNOSIS: `du -sx --block-size=1 /var/tmp`, then
    `du -sh /var/tmp/* | sort -h | tail` to name the generator. REMEDIATION: delete the
    generator's own scratch, or run `bash infra/scripts/vartmp-cap.sh --reap` to force a pass.
    Published as rebar/host:var_tmp_used_percent by observability.sh 2h.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "var_tmp_used_percent"
  statistic   = "Maximum"

  # Six high or absent five-minute periods page. Section 2h publishes
  # only after sizing /var/tmp, whose cleanup has no writer-enforced quota.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # observability.sh 2h publishes this ONLY on a successful measurement, so absence means the
  # probe could not size /var/tmp — which is exactly when this must page rather than clear to OK
  # (bug 3276 defect 2). Pinned by tests/unit/test_alarm_actions_terraform.py.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "2ba3"
  }
}

resource "aws_cloudwatch_metric_alarm" "var_tmp_cleanup_not_active" {
  alarm_name        = "rebar-var-tmp-cleanup-not-active"
  alarm_description = <<-EOT
    NOTHING is bounding /var/tmp. Either the tmpfiles drop-in is missing or no longer matches
    what infra/scripts/vartmp-cap.sh renders, or rebar-var-tmp-reaper.timer is not running — and
    with no XFS project quota in force (rebar/host:var_tmp_hard_quota_in_effect) that leaves the
    tree bounded only by the size of the root volume, which is the 2026-09-02 outage exactly.
    While this fires, rebar-var-tmp-usage-high is measured against a budget nothing is holding.
    CONFIRM with `bash infra/scripts/vartmp-cap.sh --check-active` (prints 1/0). REMEDIATE with
    `bash infra/scripts/vartmp-cap.sh --install`, which is idempotent and reports the state in
    words, then `systemctl status rebar-var-tmp-reaper.timer`. Published as
    rebar/host:var_tmp_cleanup_active, a 1/0 heartbeat on every probe tick.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "var_tmp_cleanup_active"
  statistic   = "Minimum"

  # Section 2h publishes this 1/0 cleanup heartbeat each tick. Six
  # missing or 0 readings page. One healthy 1 clears a scheduling gap.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics (the gate_scratch_unmounted / journal_cap_not_in_effect shape): the
  # healthy path publishes 1 on EVERY tick, so absence means the probe, the timer, or the host is
  # dead — the one state this alarm most needs to announce.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "2ba3"
  }
}

# observability.sh 2i publishes dimensionless writable-layer usage and reaper
# status. container-cap.sh draws one internal share from docker-storage-cap.sh's
# Docker budget. Publish but do not alarm on container_quota_enforceable until
# XFS pquota is enabled by reboot.

resource "aws_cloudwatch_metric_alarm" "container_writable_usage_high" {
  alarm_name        = "rebar-container-writable-usage-high"
  alarm_description = <<-EOT
    Writable container layers are above 85% of their share of the Docker budget (the value lives
    in infra/scripts/container-cap.sh, readable with `--print-env`). READ THIS KNOWING WHAT HOLDS
    IT: rebar-container-reaper.timer can only remove EXITED containers, so if the bytes belong to
    a RUNNING service NOTHING here bounds them — only an overlay2 per-container quota would, and
    rebar/host:container_quota_enforceable says whether this host can even have one. DIAGNOSIS:
    `docker ps -a --size` names the containers; compare rebar/host:container_exited_bytes against
    container_writable_bytes to see whether this is reclaimable debris or the live services.
    REMEDIATION: `bash infra/scripts/container-cap.sh --reap` forces a pass; a live service
    growing its own layer is an application problem, not a cleanup one. NEVER delete anything
    under /var/lib/docker/overlay2 by hand. Published as
    rebar/host:container_writable_used_percent by observability.sh 2i.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "container_writable_used_percent"
  statistic   = "Maximum"

  # Six high or absent five-minute periods page. Section 2i publishes
  # only after measuring container writable layers. Missing data stays breaching.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 85
  comparison_operator = "GreaterThanThreshold"

  # observability.sh 2i publishes this ONLY when `docker system df` yielded a parseable Containers
  # row, so absence means the probe could not size writable layers — exactly when this must page
  # rather than clear to OK (bug 3276 defect 2). Pinned by tests/unit/test_alarm_actions_terraform.py.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "910b"
  }
}

resource "aws_cloudwatch_metric_alarm" "container_reaper_not_active" {
  alarm_name        = "rebar-container-reaper-not-active"
  alarm_description = <<-EOT
    NOTHING is reaping exited-container debris. Either rebar-container-reaper.timer is not
    running or its units no longer match what infra/scripts/container-cap.sh renders — and since
    no per-container overlay2 quota is in force on this host (see
    rebar/host:container_quota_enforceable), that leaves writable layers bounded only by the size
    of the root volume, which is the 2026-09-02 outage exactly. While this fires,
    rebar-container-writable-usage-high is measured against a share nothing is holding. CONFIRM
    with `bash infra/scripts/container-cap.sh --check-active` (prints 1/0). REMEDIATE with
    `bash infra/scripts/container-cap.sh --install`, which is idempotent and reports the state in
    words, then `systemctl status rebar-container-reaper.timer`. Published as
    rebar/host:container_reaper_active, a 1/0 heartbeat on every probe tick.
    Runbook: infra/runbooks/review-bot-ops.md.
  EOT

  namespace   = "rebar/host"
  metric_name = "container_reaper_active"
  statistic   = "Minimum"

  # Section 2i publishes this 1/0 reaper heartbeat each tick. Six
  # missing or 0 readings page. This tail heartbeat also detects a truncated probe.
  period              = 300
  evaluation_periods  = 6
  datapoints_to_alarm = 6
  threshold           = 1
  comparison_operator = "LessThanThreshold"

  # Heartbeat semantics (the var_tmp_cleanup_not_active / journal_cap_not_in_effect shape): the
  # healthy path publishes 1 on EVERY tick, so absence means the probe, the timer, or the host is
  # dead — the one state this alarm most needs to announce.
  treat_missing_data = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]

  tags = {
    Project = "rebar"
    Ticket  = "910b"
  }
}
