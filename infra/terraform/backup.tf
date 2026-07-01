# ---------------------------------------------------------------------------
# backup.tf — DLM daily snapshots of the Gerrit data volume
# ---------------------------------------------------------------------------
# SINGLE-OWNER CONTRACT: S1 OWNS this DLM lifecycle policy AND its execution
# role (rebar-dlm-lifecycle-role, defined in iam.tf). S7 only MONITORS the
# resulting snapshots/alarms — it must NOT declare a second aws_dlm_lifecycle_policy.
# ---------------------------------------------------------------------------

resource "aws_dlm_lifecycle_policy" "gerrit_data" {
  description        = "Daily snapshots of the rebar Gerrit data volume"
  execution_role_arn = aws_iam_role.dlm.arn
  state              = "ENABLED"

  policy_details {
    resource_types = ["VOLUME"]

    # Target the data volume by its Name tag (matches aws_ebs_volume.data).
    target_tags = {
      Name = "rebar-gerrit-data"
    }

    schedule {
      name = "daily-snapshots"

      create_rule {
        interval      = 24
        interval_unit = "HOURS"
        times         = ["03:00"]
      }

      retain_rule {
        count = var.snapshot_retention_count
      }

      tags_to_add = {
        SnapshotCreator = "rebar-dlm"
      }

      copy_tags = true
    }
  }

  tags = {
    Project = "rebar"
  }
}
