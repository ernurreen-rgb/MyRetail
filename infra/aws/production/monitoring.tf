resource "aws_sns_topic" "operations" {
  name              = "${var.name_prefix}-operations"
  kms_master_key_id = aws_kms_key.application.arn
}

data "aws_iam_policy_document" "operations_topic" {
  statement {
    sid       = "AccountOwnerAdministration"
    effect    = "Allow"
    actions   = ["sns:*"]
    resources = [aws_sns_topic.operations.arn]

    principals {
      type        = "AWS"
      identifiers = ["arn:${data.aws_partition.current.partition}:iam::${data.aws_caller_identity.current.account_id}:root"]
    }
  }

  statement {
    sid       = "CloudWatchAlarmPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operations.arn]

    principals {
      type        = "Service"
      identifiers = ["cloudwatch.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:cloudwatch:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alarm:${var.name_prefix}-*"]
    }
  }

  statement {
    sid       = "RDSEventPublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operations.arn]

    principals {
      type        = "Service"
      identifiers = ["events.rds.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:${data.aws_partition.current.partition}:rds:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster:${var.name_prefix}-state"]
    }
  }

  statement {
    sid       = "EventBridgePublish"
    effect    = "Allow"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.operations.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.backup_failure.arn]
    }
  }
}

resource "aws_sns_topic_policy" "operations" {
  arn    = aws_sns_topic.operations.arn
  policy = data.aws_iam_policy_document.operations_topic.json
}

resource "aws_cloudwatch_log_group" "rds_postgresql" {
  name              = "/aws/rds/cluster/${var.name_prefix}-state/postgresql"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.application.arn
}

resource "aws_sns_topic_subscription" "operations_email" {
  count = var.alarm_email == null ? 0 : 1

  topic_arn = aws_sns_topic.operations.arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_db_event_subscription" "state" {
  name      = "${var.name_prefix}-database"
  sns_topic = aws_sns_topic.operations.arn

  source_type = "db-cluster"
  source_ids  = [aws_rds_cluster.state.id]
  event_categories = [
    "availability",
    "failover",
    "failure",
    "maintenance",
    "notification",
  ]

  depends_on = [aws_sns_topic_policy.operations]
}

resource "aws_cloudwatch_event_rule" "state_monitor" {
  name                = "${var.name_prefix}-state-monitor"
  description         = "Run the least-privilege database and recovery-age monitor every five minutes"
  schedule_expression = "rate(5 minutes)"
  state               = var.monitoring_enabled ? "ENABLED" : "DISABLED"
}

resource "aws_cloudwatch_event_target" "state_monitor" {
  rule     = aws_cloudwatch_event_rule.state_monitor.name
  arn      = aws_ecs_cluster.main.arn
  role_arn = aws_iam_role.events_ecs.arn

  ecs_target {
    task_definition_arn = aws_ecs_task_definition.monitor.arn
    task_count          = 1
    launch_type         = "FARGATE"
    platform_version    = "1.4.0"

    network_configuration {
      assign_public_ip = false
      security_groups  = [aws_security_group.api.id]
      subnets          = aws_subnet.app[*].id
    }
  }
}

resource "aws_cloudwatch_event_rule" "backup_failure" {
  name        = "${var.name_prefix}-backup-failure"
  description = "Alert when the periodic AWS Backup snapshot does not complete"
  event_pattern = jsonencode({
    source      = ["aws.backup"]
    detail-type = ["Backup Job State Change"]
    detail = {
      resourceArn = [aws_rds_cluster.state.arn]
      state       = ["ABORTED", "EXPIRED", "FAILED"]
    }
  })
}

resource "aws_cloudwatch_event_target" "backup_failure" {
  rule = aws_cloudwatch_event_rule.backup_failure.name
  arn  = aws_sns_topic.operations.arn

  depends_on = [aws_sns_topic_policy.operations]
}

resource "aws_cloudwatch_log_metric_filter" "application_monitor" {
  for_each = {
    DatabaseUnavailable = "MYRETAIL_MONITOR_DATABASE_UNAVAILABLE"
    MigrationMismatch   = "MYRETAIL_MONITOR_MIGRATION_MISMATCH"
    RecoveryAge         = "MYRETAIL_MONITOR_RECOVERY_AGE"
  }

  name           = "${var.name_prefix}-${each.key}"
  pattern        = "\"${each.value}\""
  log_group_name = aws_cloudwatch_log_group.migration.name

  metric_transformation {
    name          = each.key
    namespace     = "MyRetail/Production"
    value         = "1"
    default_value = 0
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "postgres_timeout" {
  for_each = {
    LockTimeout      = "canceling statement due to lock timeout"
    StatementTimeout = "canceling statement due to statement timeout"
  }

  name           = "${var.name_prefix}-${each.key}"
  pattern        = "\"${each.value}\""
  log_group_name = aws_cloudwatch_log_group.rds_postgresql.name

  metric_transformation {
    name          = each.key
    namespace     = "MyRetail/Production"
    value         = "1"
    default_value = 0
    unit          = "Count"
  }
}

resource "aws_cloudwatch_metric_alarm" "application_monitor" {
  for_each = toset([
    "DatabaseUnavailable",
    "LockTimeout",
    "MigrationMismatch",
    "RecoveryAge",
    "StatementTimeout",
  ])

  alarm_name          = "${var.name_prefix}-${lower(each.key)}"
  alarm_description   = "MyRetail required production signal: ${each.key}"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 1
  datapoints_to_alarm = 1
  metric_name         = each.key
  namespace           = "MyRetail/Production"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.monitoring_enabled
  alarm_actions       = [aws_sns_topic.operations.arn]
  ok_actions          = [aws_sns_topic.operations.arn]

  depends_on = [
    aws_cloudwatch_log_metric_filter.application_monitor,
    aws_cloudwatch_log_metric_filter.postgres_timeout,
  ]
}

resource "aws_cloudwatch_metric_alarm" "api_running_tasks" {
  alarm_name          = "${var.name_prefix}-api-running-tasks"
  alarm_description   = "API running task count is below the approved production replica floor"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "RunningTaskCount"
  namespace           = "ECS/ContainerInsights"
  period              = 60
  statistic           = "Minimum"
  threshold           = var.api_desired_count
  treat_missing_data  = "breaching"
  actions_enabled     = var.traffic_enabled
  alarm_actions       = [aws_sns_topic.operations.arn]
  ok_actions          = [aws_sns_topic.operations.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.api.name
  }
}

resource "aws_cloudwatch_metric_alarm" "web_running_tasks" {
  alarm_name          = "${var.name_prefix}-web-running-tasks"
  alarm_description   = "Web running task count is below the approved production replica floor"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "RunningTaskCount"
  namespace           = "ECS/ContainerInsights"
  period              = 60
  statistic           = "Minimum"
  threshold           = var.web_desired_count
  treat_missing_data  = "breaching"
  actions_enabled     = var.traffic_enabled
  alarm_actions       = [aws_sns_topic.operations.arn]
  ok_actions          = [aws_sns_topic.operations.arn]

  dimensions = {
    ClusterName = aws_ecs_cluster.main.name
    ServiceName = aws_ecs_service.web.name
  }
}

resource "aws_cloudwatch_metric_alarm" "alb_target_5xx" {
  alarm_name          = "${var.name_prefix}-alb-target-5xx"
  alarm_description   = "Web targets returned server errors"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  metric_name         = "HTTPCode_Target_5XX_Count"
  namespace           = "AWS/ApplicationELB"
  period              = 60
  statistic           = "Sum"
  threshold           = 5
  treat_missing_data  = "notBreaching"
  actions_enabled     = var.traffic_enabled
  alarm_actions       = [aws_sns_topic.operations.arn]
  ok_actions          = [aws_sns_topic.operations.arn]

  dimensions = {
    LoadBalancer = aws_lb.web.arn_suffix
    TargetGroup  = aws_lb_target_group.web.arn_suffix
  }
}

resource "aws_cloudwatch_metric_alarm" "database_cpu" {
  alarm_name          = "${var.name_prefix}-database-cpu"
  alarm_description   = "RDS Multi-AZ DB cluster CPU is persistently high"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 5
  datapoints_to_alarm = 3
  metric_name         = "CPUUtilization"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 85
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.operations.arn]
  ok_actions          = [aws_sns_topic.operations.arn]

  dimensions = {
    DBClusterIdentifier = aws_rds_cluster.state.cluster_identifier
  }
}

resource "aws_cloudwatch_metric_alarm" "database_replica_lag" {
  alarm_name          = "${var.name_prefix}-database-replica-lag"
  alarm_description   = "RDS Multi-AZ DB cluster replica lag exceeds the production threshold"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  metric_name         = "ReplicaLag"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 60
  treat_missing_data  = "breaching"
  alarm_actions       = [aws_sns_topic.operations.arn]
  ok_actions          = [aws_sns_topic.operations.arn]

  dimensions = {
    DBClusterIdentifier = aws_rds_cluster.state.cluster_identifier
  }
}

resource "aws_cloudwatch_metric_alarm" "database_connections" {
  alarm_name          = "${var.name_prefix}-database-pool-saturation"
  alarm_description   = "RDS connections exceed the reviewed application pool saturation threshold"
  comparison_operator = "GreaterThanOrEqualToThreshold"
  evaluation_periods  = 3
  datapoints_to_alarm = 2
  metric_name         = "DatabaseConnections"
  namespace           = "AWS/RDS"
  period              = 60
  statistic           = "Maximum"
  threshold           = var.database_connections_alarm_threshold
  treat_missing_data  = "breaching"
  actions_enabled     = var.monitoring_enabled
  alarm_actions       = [aws_sns_topic.operations.arn]
  ok_actions          = [aws_sns_topic.operations.arn]

  dimensions = {
    DBClusterIdentifier = aws_rds_cluster.state.cluster_identifier
  }
}

resource "aws_cloudwatch_dashboard" "main" {
  dashboard_name = var.name_prefix
  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "ECS running tasks"
          region = var.aws_region
          metrics = [
            ["ECS/ContainerInsights", "RunningTaskCount", "ServiceName", aws_ecs_service.api.name, "ClusterName", aws_ecs_cluster.main.name],
            [".", ".", ".", aws_ecs_service.web.name, ".", "."],
          ]
          stat   = "Minimum"
          period = 60
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "RDS CPU and replica lag"
          region = var.aws_region
          metrics = [
            ["AWS/RDS", "CPUUtilization", "DBClusterIdentifier", aws_rds_cluster.state.cluster_identifier],
            [".", "ReplicaLag", ".", ".", { yAxis = "right" }],
          ]
          stat   = "Maximum"
          period = 60
        }
      },
    ]
  })
}
