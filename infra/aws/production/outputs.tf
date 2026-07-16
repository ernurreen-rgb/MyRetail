output "aws_account_id" {
  description = "AWS account selected by the deployment credentials."
  value       = data.aws_caller_identity.current.account_id
}

output "web_url" {
  description = "Public production URL. It remains without healthy targets while traffic_enabled is false."
  value       = local.web_origin
}

output "ecs_cluster_name" {
  description = "Cluster used by services and controlled one-shot tasks."
  value       = aws_ecs_cluster.main.name
}

output "app_subnet_ids" {
  description = "Private subnet IDs passed to migration and preflight run-task commands."
  value       = aws_subnet.app[*].id
}

output "api_security_group_id" {
  description = "Security group passed to controlled migration and preflight tasks."
  value       = aws_security_group.api.id
}

output "migration_task_definition_arn" {
  description = "One-shot migration task pinned to the reviewed schema revision."
  value       = aws_ecs_task_definition.migration.arn
}

output "database_bootstrap_task_definition_arn" {
  description = "One-shot PostgreSQL role-bootstrap task; run before the first migration."
  value       = aws_ecs_task_definition.database_bootstrap.arn
}

output "preflight_task_definition_arn" {
  description = "One-shot least-privilege PostgreSQL preflight task."
  value       = aws_ecs_task_definition.preflight.arn
}

output "monitor_task_definition_arn" {
  description = "Scheduled application-role database/recovery monitor task."
  value       = aws_ecs_task_definition.monitor.arn
}

output "database_endpoint" {
  description = "Writer endpoint used when external automation creates role-specific database URLs."
  value       = aws_rds_cluster.state.endpoint
}

output "database_master_secret_arn" {
  description = "RDS-managed master secret for the initial role bootstrap only."
  value       = one(aws_rds_cluster.state.master_user_secret).secret_arn
}

output "runtime_secret_arns" {
  description = "Empty secret containers that must be populated out-of-band before tasks run."
  value = {
    auth            = aws_secretsmanager_secret.auth.arn
    erpnext         = aws_secretsmanager_secret.erpnext.arn
    state_app       = aws_secretsmanager_secret.state_app.arn
    state_migration = aws_secretsmanager_secret.state_migration.arn
  }
}

output "ecr_repository_urls" {
  description = "Destination repositories for immutable production images."
  value = {
    api                = aws_ecr_repository.api.repository_url
    database_bootstrap = aws_ecr_repository.database_bootstrap.repository_url
    migration          = aws_ecr_repository.migration.repository_url
    web                = aws_ecr_repository.web.repository_url
  }
}

output "operations_topic_arn" {
  description = "Operational alert topic; email subscriptions still require recipient confirmation."
  value       = aws_sns_topic.operations.arn
}

output "backup_plan_arn" {
  description = "Daily periodic snapshot plan supplementing native RDS PITR."
  value       = aws_backup_plan.state.arn
}

output "backup_vault_name" {
  description = "Locked vault used by periodic RDS cluster snapshots and restore evidence."
  value       = aws_backup_vault.state.name
}

output "backup_restore_role_arn" {
  description = "Role used by controlled isolated restore jobs."
  value       = aws_iam_role.backup.arn
}

output "traffic_enabled" {
  description = "Effective fail-closed traffic latch."
  value       = var.traffic_enabled
}
