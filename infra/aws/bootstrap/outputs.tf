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
