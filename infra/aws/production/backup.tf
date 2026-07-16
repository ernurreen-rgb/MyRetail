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

data "aws_iam_policy_document" "backup_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["backup.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "backup" {
  name               = "${var.name_prefix}-backup"
  assume_role_policy = data.aws_iam_policy_document.backup_assume_role.json
}

resource "aws_iam_role_policy_attachment" "backup" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "restore" {
  role       = aws_iam_role.backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

data "aws_iam_policy_document" "backup_kms" {
  statement {
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:GenerateDataKey*",
    ]
    resources = [aws_kms_key.application.arn]
  }

  statement {
    effect    = "Allow"
    actions   = ["kms:CreateGrant"]
    resources = [aws_kms_key.application.arn]

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "backup_kms" {
  name   = "state-backup-kms"
  role   = aws_iam_role.backup.id
  policy = data.aws_iam_policy_document.backup_kms.json
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
  iam_role_arn = aws_iam_role.backup.arn
  resources    = [aws_rds_cluster.state.arn]
}
