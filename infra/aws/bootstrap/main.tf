data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

resource "aws_kms_key" "state" {
  description             = "MyRetail production Terraform state"
  deletion_window_in_days = 30
  enable_key_rotation     = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_kms_alias" "state" {
  name          = "alias/myretail-production-terraform-state"
  target_key_id = aws_kms_key.state.key_id
}

resource "aws_s3_bucket" "state" {
  bucket        = var.state_bucket_name
  force_destroy = false

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      kms_master_key_id = aws_kms_key.state.arn
      sse_algorithm     = "aws:kms"
    }

    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    id     = "retain-noncurrent-state"
    status = "Enabled"

    filter {}

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }

    noncurrent_version_expiration {
      noncurrent_days = 365
    }
  }

  depends_on = [aws_s3_bucket_versioning.state]
}

data "aws_iam_policy_document" "state" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid    = "DenyUnencryptedObjectWrites"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.state.arn}/*"]

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption"
      values   = ["aws:kms"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state.json

  depends_on = [aws_s3_bucket_public_access_block.state]
}

resource "aws_iam_openid_connect_provider" "github" {
  url             = "https://token.actions.githubusercontent.com"
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = []
}

locals {
  production_name_prefix = "myretail-production"
  production_role_names = {
    api_task       = "myretail-production-api-task"
    backup         = "myretail-production-backup"
    ecs_execution  = "myretail-production-ecs-execution"
    events_ecs     = "myretail-production-events-ecs"
    migration_task = "myretail-production-migration-task"
    web_task       = "myretail-production-web-task"
  }
  production_role_arns = {
    for key, name in local.production_role_names :
    key => "arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:role/${name}"
  }
}

data "aws_iam_policy_document" "ecs_tasks_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "production_ecs_execution" {
  name               = local.production_role_names.ecs_execution
  description        = "Fetch immutable MyRetail images, logs and runtime secrets for ECS tasks"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role" "production_api_task" {
  name               = local.production_role_names.api_task
  description        = "MyRetail API task role without AWS control-plane permissions"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role" "production_web_task" {
  name               = local.production_role_names.web_task
  description        = "MyRetail web task role without AWS control-plane permissions"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role" "production_migration_task" {
  name               = local.production_role_names.migration_task
  description        = "MyRetail controlled database task role without AWS control-plane permissions"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

data "aws_iam_policy_document" "production_ecs_execution" {
  statement {
    sid       = "GetECRAuthorizationToken"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "PullMyRetailImages"
    effect = "Allow"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:ecr:*:${data.aws_caller_identity.current.account_id}:repository/${local.production_name_prefix}/*",
    ]
  }

  statement {
    sid    = "WriteMyRetailTaskLogs"
    effect = "Allow"
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:logs:*:${data.aws_caller_identity.current.account_id}:log-group:/ecs/${local.production_name_prefix}/*:*",
    ]
  }

  statement {
    sid     = "ReadMyRetailRuntimeSecrets"
    effect  = "Allow"
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      "arn:${data.aws_partition.current.partition}:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:${local.production_name_prefix}/*",
      "arn:${data.aws_partition.current.partition}:secretsmanager:*:${data.aws_caller_identity.current.account_id}:secret:rds!*",
    ]
  }

  statement {
    sid     = "DecryptMyRetailRuntimeSecrets"
    effect  = "Allow"
    actions = ["kms:Decrypt"]
    resources = [
      "arn:${data.aws_partition.current.partition}:kms:*:${data.aws_caller_identity.current.account_id}:key/*",
    ]

    condition {
      test     = "ForAnyValue:StringLike"
      variable = "kms:ResourceAliases"
      values   = ["alias/${local.production_name_prefix}*"]
    }

    condition {
      test     = "StringLike"
      variable = "kms:ViaService"
      values   = ["secretsmanager.*.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "production_ecs_execution" {
  name   = "myretail-production-runtime-bootstrap"
  role   = aws_iam_role.production_ecs_execution.id
  policy = data.aws_iam_policy_document.production_ecs_execution.json
}

data "aws_iam_policy_document" "events_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "production_events_ecs" {
  name               = local.production_role_names.events_ecs
  description        = "Run only the scheduled MyRetail recovery monitor task"
  assume_role_policy = data.aws_iam_policy_document.events_assume_role.json
}

data "aws_iam_policy_document" "production_events_ecs" {
  statement {
    sid     = "RunMyRetailStateMonitor"
    effect  = "Allow"
    actions = ["ecs:RunTask"]
    resources = [
      "arn:${data.aws_partition.current.partition}:ecs:*:${data.aws_caller_identity.current.account_id}:task-definition/${local.production_name_prefix}-monitor:*",
    ]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values = [
        "arn:${data.aws_partition.current.partition}:ecs:*:${data.aws_caller_identity.current.account_id}:cluster/${local.production_name_prefix}",
      ]
    }
  }

  statement {
    sid     = "PassMyRetailMonitorRolesToECS"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      local.production_role_arns.api_task,
      local.production_role_arns.ecs_execution,
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "production_events_ecs" {
  name   = "myretail-production-run-state-monitor"
  role   = aws_iam_role.production_events_ecs.id
  policy = data.aws_iam_policy_document.production_events_ecs.json
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

resource "aws_iam_role" "production_backup" {
  name               = local.production_role_names.backup
  description        = "AWS Backup service role for the isolated MyRetail production database"
  assume_role_policy = data.aws_iam_policy_document.backup_assume_role.json
}

resource "aws_iam_role_policy_attachment" "production_backup" {
  role       = aws_iam_role.production_backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForBackup"
}

resource "aws_iam_role_policy_attachment" "production_restore" {
  role       = aws_iam_role.production_backup.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AWSBackupServiceRolePolicyForRestores"
}

data "aws_iam_policy_document" "production_backup_kms" {
  statement {
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:GenerateDataKey*",
    ]
    resources = [
      "arn:${data.aws_partition.current.partition}:kms:*:${data.aws_caller_identity.current.account_id}:key/*",
    ]

    condition {
      test     = "ForAnyValue:StringLike"
      variable = "kms:ResourceAliases"
      values   = ["alias/${local.production_name_prefix}*"]
    }
  }

  statement {
    effect  = "Allow"
    actions = ["kms:CreateGrant"]
    resources = [
      "arn:${data.aws_partition.current.partition}:kms:*:${data.aws_caller_identity.current.account_id}:key/*",
    ]

    condition {
      test     = "ForAnyValue:StringLike"
      variable = "kms:ResourceAliases"
      values   = ["alias/${local.production_name_prefix}*"]
    }

    condition {
      test     = "Bool"
      variable = "kms:GrantIsForAWSResource"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "production_backup_kms" {
  name   = "myretail-production-backup-kms"
  role   = aws_iam_role.production_backup.id
  policy = data.aws_iam_policy_document.production_backup_kms.json
}

data "aws_iam_policy_document" "github_production_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "github_production" {
  name                 = "myretail-deployment-github-oidc"
  description          = "Terraform production deployment from the protected GitHub environment"
  assume_role_policy   = data.aws_iam_policy_document.github_production_assume_role.json
  max_session_duration = 3600
}

data "aws_iam_policy_document" "github_production" {
  statement {
    sid    = "ManageMyRetailProductionServices"
    effect = "Allow"
    actions = [
      "acm:DescribeCertificate",
      "acm:ListCertificates",
      "backup:*",
      "cloudwatch:*",
      "ec2:*",
      "ecr:*",
      "ecs:*",
      "elasticloadbalancing:*",
      "events:*",
      "kms:CancelKeyDeletion",
      "kms:CreateAlias",
      "kms:CreateKey",
      "kms:DeleteAlias",
      "kms:DescribeKey",
      "kms:DisableKey",
      "kms:DisableKeyRotation",
      "kms:EnableKey",
      "kms:EnableKeyRotation",
      "kms:GetKeyPolicy",
      "kms:GetKeyRotationStatus",
      "kms:ListAliases",
      "kms:ListResourceTags",
      "kms:PutKeyPolicy",
      "kms:ScheduleKeyDeletion",
      "kms:TagResource",
      "kms:UntagResource",
      "kms:UpdateAlias",
      "kms:UpdateKeyDescription",
      "logs:*",
      "rds:*",
      "route53:*",
      "secretsmanager:CancelRotateSecret",
      "secretsmanager:CreateSecret",
      "secretsmanager:DeleteResourcePolicy",
      "secretsmanager:DeleteSecret",
      "secretsmanager:DescribeSecret",
      "secretsmanager:GetResourcePolicy",
      "secretsmanager:ListSecretVersionIds",
      "secretsmanager:PutResourcePolicy",
      "secretsmanager:RestoreSecret",
      "secretsmanager:RotateSecret",
      "secretsmanager:StopReplicationToReplica",
      "secretsmanager:TagResource",
      "secretsmanager:UntagResource",
      "secretsmanager:UpdateSecret",
      "servicediscovery:*",
      "sns:*",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "ReadPreprovisionedProductionRoles"
    effect = "Allow"
    actions = [
      "iam:GetRole",
      "iam:GetRolePolicy",
      "iam:ListAttachedRolePolicies",
      "iam:ListRolePolicies",
    ]
    resources = values(local.production_role_arns)
  }

  statement {
    sid     = "PassPreprovisionedTaskRolesOnlyToECS"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      local.production_role_arns.api_task,
      local.production_role_arns.ecs_execution,
      local.production_role_arns.migration_task,
      local.production_role_arns.web_task,
    ]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }

  statement {
    sid       = "PassPreprovisionedEventsRoleOnlyToEventBridge"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [local.production_role_arns.events_ecs]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["events.amazonaws.com"]
    }
  }

  statement {
    sid       = "PassPreprovisionedBackupRoleOnlyToBackup"
    effect    = "Allow"
    actions   = ["iam:PassRole"]
    resources = [local.production_role_arns.backup]

    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["backup.amazonaws.com"]
    }
  }

  statement {
    sid       = "CreateRequiredServiceLinkedRoles"
    effect    = "Allow"
    actions   = ["iam:CreateServiceLinkedRole"]
    resources = ["*"]

    condition {
      test     = "StringLike"
      variable = "iam:AWSServiceName"
      values = [
        "ecs.amazonaws.com",
        "elasticloadbalancing.amazonaws.com",
        "rds.amazonaws.com",
      ]
    }
  }

  statement {
    sid    = "ReadWriteLockedTerraformState"
    effect = "Allow"
    actions = [
      "s3:DeleteObject",
      "s3:GetObject",
      "s3:ListBucket",
      "s3:PutObject",
    ]
    resources = [
      aws_s3_bucket.state.arn,
      "${aws_s3_bucket.state.arn}/*",
    ]
  }

  statement {
    sid    = "UseTerraformStateKey"
    effect = "Allow"
    actions = [
      "kms:Decrypt",
      "kms:DescribeKey",
      "kms:Encrypt",
      "kms:GenerateDataKey",
    ]
    resources = [aws_kms_key.state.arn]
  }
}

resource "aws_iam_role_policy" "github_production" {
  name   = "myretail-production-terraform"
  role   = aws_iam_role.github_production.id
  policy = data.aws_iam_policy_document.github_production.json
}
