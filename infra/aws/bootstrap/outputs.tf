output "state_bucket_name" {
  description = "S3 bucket used by the production root module."
  value       = aws_s3_bucket.state.id
}

output "state_kms_key_arn" {
  description = "KMS key passed to the S3 backend as kms_key_id."
  value       = aws_kms_key.state.arn
}

output "aws_account_id" {
  description = "Account in which the production state is protected."
  value       = data.aws_caller_identity.current.account_id
}

output "github_production_role_arn" {
  description = "Set this as the protected GitHub production environment variable AWS_ROLE_ARN."
  value       = aws_iam_role.github_production.arn
}

output "production_runtime_role_arns" {
  description = "Pre-provisioned least-privilege roles consumed read-only by the production root."
  value = {
    api_task       = aws_iam_role.production_api_task.arn
    backup         = aws_iam_role.production_backup.arn
    ecs_execution  = aws_iam_role.production_ecs_execution.arn
    events_ecs     = aws_iam_role.production_events_ecs.arn
    migration_task = aws_iam_role.production_migration_task.arn
    web_task       = aws_iam_role.production_web_task.arn
  }
}
