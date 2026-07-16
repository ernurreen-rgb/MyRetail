variable "aws_region" {
  description = "AWS region that owns the production Terraform state bucket."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$", var.aws_region))
    error_message = "aws_region must be an AWS region identifier."
  }
}

variable "state_bucket_name" {
  description = "Globally unique S3 bucket name for the MyRetail production state."
  type        = string

  validation {
    condition = (
      length(var.state_bucket_name) >= 3 &&
      length(var.state_bucket_name) <= 63 &&
      can(regex("^[a-z0-9][a-z0-9.-]*[a-z0-9]$", var.state_bucket_name)) &&
      !can(regex("[.][.]", var.state_bucket_name))
    )
    error_message = "state_bucket_name must be a valid S3 bucket name."
  }
}

variable "github_repository" {
  description = "Exact GitHub owner/repository allowed to assume the production deployment role."
  type        = string
  default     = "ernurreen-rgb/MyRetail"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must use owner/repository form."
  }
}

variable "github_environment" {
  description = "Protected GitHub environment required by the OIDC subject claim."
  type        = string
  default     = "production"

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+$", var.github_environment))
    error_message = "github_environment must be a GitHub environment name."
  }
}
