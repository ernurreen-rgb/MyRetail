data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

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

resource "aws_iam_role" "ecs_execution" {
  name               = "${var.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:${data.aws_partition.current.partition}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_execution_secrets" {
  statement {
    sid    = "ReadRuntimeSecrets"
    effect = "Allow"
    actions = [
      "secretsmanager:GetSecretValue",
    ]
    resources = [
      aws_secretsmanager_secret.auth.arn,
      aws_secretsmanager_secret.state_app.arn,
      aws_secretsmanager_secret.state_migration.arn,
      aws_secretsmanager_secret.erpnext.arn,
      one(aws_rds_cluster.state.master_user_secret).secret_arn,
    ]
  }

  statement {
    sid       = "DecryptRuntimeSecrets"
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [aws_kms_key.application.arn]
  }
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "runtime-secrets"
  role   = aws_iam_role.ecs_execution.id
  policy = data.aws_iam_policy_document.ecs_execution_secrets.json
}

resource "aws_iam_role" "api_task" {
  name               = "${var.name_prefix}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role" "web_task" {
  name               = "${var.name_prefix}-web-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
}

resource "aws_iam_role" "migration_task" {
  name               = "${var.name_prefix}-migration-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_assume_role.json
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

resource "aws_iam_role" "events_ecs" {
  name               = "${var.name_prefix}-events-ecs"
  assume_role_policy = data.aws_iam_policy_document.events_assume_role.json
}

data "aws_iam_policy_document" "events_ecs" {
  statement {
    sid       = "RunStateMonitor"
    effect    = "Allow"
    actions   = ["ecs:RunTask"]
    resources = [aws_ecs_task_definition.monitor.arn]

    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }

  statement {
    sid     = "PassMonitorRoles"
    effect  = "Allow"
    actions = ["iam:PassRole"]
    resources = [
      aws_iam_role.api_task.arn,
      aws_iam_role.ecs_execution.arn,
    ]
  }
}

resource "aws_iam_role_policy" "events_ecs" {
  name   = "run-state-monitor"
  role   = aws_iam_role.events_ecs.id
  policy = data.aws_iam_policy_document.events_ecs.json
}
