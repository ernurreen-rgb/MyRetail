resource "aws_backup_vault" "state" {
  name        = "${var.name_prefix}-state"
  kms_key_arn = aws_kms_key.application.arn
}

resource "aws_backup_vault_lock_configuration" "state" {
  backup_vault_name   = aws_backup_vault.state.name
  min_retention_days  = var.backup_retention_days
  max_retention_days  = 35
  changeable_for_days = 3
}

resource "aws_backup_plan" "state" {
  name = "${var.name_prefix}-state"

  rule {
    rule_name         = "daily-rds-cluster-snapshot"
    target_vault_name = aws_backup_vault.state.name
    schedule          = "cron(0 5 * * ? *)"
    start_window      = 60
    completion_window = 180

    lifecycle {
      delete_after = var.backup_retention_days
    }
  }
}

resource "aws_backup_selection" "state" {
  name         = "${var.name_prefix}-state"
  plan_id      = aws_backup_plan.state.id
  iam_role_arn = data.aws_iam_role.backup.arn
  resources    = [aws_rds_cluster.state.arn]
}
