data "aws_iam_policy_document" "application_kms" {
  statement {
    sid       = "EnableAccountIAMPolicies"
    effect    = "Allow"
    actions   = ["kms:*"]
    resources = ["*"]

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid    = "AllowCloudWatchLogsEncryption"
    effect = "Allow"
    actions = [
      "kms:Decrypt*",
      "kms:Describe*",
      "kms:Encrypt*",
      "kms:GenerateDataKey*",
      "kms:ReEncrypt*",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["logs.${var.aws_region}.amazonaws.com"]
    }

    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${data.aws_partition.current.partition}:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:*"]
    }
  }

  statement {
    sid    = "AllowSNSMessageEncryption"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
    ]
    resources = ["*"]

    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "kms:EncryptionContext:aws:sns:topicArn"
      values   = ["arn:${data.aws_partition.current.partition}:sns:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${var.name_prefix}-operations"]
    }
  }

  statement {
    sid    = "AllowProductionAlertPublishers"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:GenerateDataKey*",
    ]
    resources = ["*"]

    principals {
      type = "Service"
      identifiers = [
        "cloudwatch.amazonaws.com",
        "events.amazonaws.com",
        "events.rds.amazonaws.com",
      ]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values = [
        "arn:${data.aws_partition.current.partition}:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:${var.name_prefix}-*",
        "arn:${data.aws_partition.current.partition}:events:${var.aws_region}:${data.aws_caller_identity.current.account_id}:rule/${var.name_prefix}-backup-failure",
        "arn:${data.aws_partition.current.partition}:rds:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster:${var.name_prefix}-state",
      ]
    }
  }
}

resource "aws_kms_key" "application" {
  description             = "MyRetail production data, secrets, logs and container images"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.application_kms.json

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "application" {
  name          = "alias/${var.name_prefix}"
  target_key_id = aws_kms_key.application.key_id
}

resource "aws_ecr_repository" "api" {
  name                 = "${var.name_prefix}/api"
  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.application.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "migration" {
  name                 = "${var.name_prefix}/migration"
  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.application.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "database_bootstrap" {
  name                 = "${var.name_prefix}/database-bootstrap"
  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.application.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_repository" "web" {
  name                 = "${var.name_prefix}/web"
  image_tag_mutability = "IMMUTABLE"

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.application.arn
  }

  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "repositories" {
  for_each = {
    api                = aws_ecr_repository.api.name
    database_bootstrap = aws_ecr_repository.database_bootstrap.name
    migration          = aws_ecr_repository.migration.name
    web                = aws_ecr_repository.web.name
  }

  repository = each.value
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Retain the newest 30 release images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 30
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_secretsmanager_secret" "auth" {
  name                    = "${var.name_prefix}/auth"
  description             = "JSON: auth_secret and auth_rate_limit_secret"
  kms_key_id              = aws_kms_key.application.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret" "state_app" {
  name                    = "${var.name_prefix}/state-app"
  description             = "JSON: least-privilege application password and database_url"
  kms_key_id              = aws_kms_key.application.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret" "state_migration" {
  name                    = "${var.name_prefix}/state-migration"
  description             = "JSON: migrator password and database_url used only by controlled migrations"
  kms_key_id              = aws_kms_key.application.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret" "erpnext" {
  name                    = "${var.name_prefix}/erpnext"
  description             = "JSON: API keys and dedicated POS identity/assignment maps"
  kms_key_id              = aws_kms_key.application.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_rotation" "auth" {
  count = var.auth_rotation_lambda_arn == null ? 0 : 1

  secret_id           = aws_secretsmanager_secret.auth.id
  rotation_lambda_arn = var.auth_rotation_lambda_arn

  rotation_rules {
    automatically_after_days = 30
  }
}

resource "aws_secretsmanager_secret_rotation" "state_app" {
  count = var.state_app_rotation_lambda_arn == null ? 0 : 1

  secret_id           = aws_secretsmanager_secret.state_app.id
  rotation_lambda_arn = var.state_app_rotation_lambda_arn

  rotation_rules {
    automatically_after_days = 30
  }
}

resource "aws_secretsmanager_secret_rotation" "state_migration" {
  count = var.state_migration_rotation_lambda_arn == null ? 0 : 1

  secret_id           = aws_secretsmanager_secret.state_migration.id
  rotation_lambda_arn = var.state_migration_rotation_lambda_arn

  rotation_rules {
    automatically_after_days = 30
  }
}

resource "aws_secretsmanager_secret_rotation" "erpnext" {
  count = var.erpnext_rotation_lambda_arn == null ? 0 : 1

  secret_id           = aws_secretsmanager_secret.erpnext.id
  rotation_lambda_arn = var.erpnext_rotation_lambda_arn

  rotation_rules {
    automatically_after_days = 30
  }
}
