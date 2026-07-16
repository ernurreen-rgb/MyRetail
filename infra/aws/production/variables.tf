variable "aws_region" {
  description = "AWS region for the isolated tenant stack."
  type        = string

  validation {
    condition     = can(regex("^[a-z]{2}(?:-gov)?-[a-z]+-[0-9]$", var.aws_region))
    error_message = "aws_region must be an AWS region identifier."
  }
}

variable "name_prefix" {
  description = "Lowercase resource prefix, unique within the AWS account and region."
  type        = string
  default     = "myretail-production"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,31}$", var.name_prefix))
    error_message = "name_prefix must contain 3-32 lowercase letters, digits, or hyphens."
  }
}

variable "tenant_id" {
  description = "Immutable UUID for the only tenant served by this stack."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", var.tenant_id))
    error_message = "tenant_id must be a lowercase RFC 9562 UUID."
  }
}

variable "tenant_slug" {
  description = "Stable tenant slug enforced by the isolated API deployment."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,62}$", var.tenant_slug))
    error_message = "tenant_slug must contain lowercase letters, digits, or hyphens."
  }
}

variable "tenant_route_version" {
  description = "Monotonic fixed-route version embedded in auth claims."
  type        = number
  default     = 1

  validation {
    condition     = var.tenant_route_version >= 1 && floor(var.tenant_route_version) == var.tenant_route_version
    error_message = "tenant_route_version must be a positive integer."
  }
}

variable "availability_zones" {
  description = "Exactly three distinct AZs required by the RDS Multi-AZ DB cluster."
  type        = list(string)

  validation {
    condition = (
      length(var.availability_zones) == 3 &&
      length(toset(var.availability_zones)) == 3 &&
      alltrue([for zone in var.availability_zones : startswith(zone, var.aws_region)])
    )
    error_message = "availability_zones must contain three distinct AZs from aws_region."
  }
}

variable "vpc_cidr" {
  description = "VPC CIDR."
  type        = string
  default     = "10.42.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr))
    error_message = "vpc_cidr must be a valid CIDR block."
  }
}

variable "public_subnet_cidrs" {
  description = "Three ALB/NAT public subnet CIDRs, ordered like availability_zones."
  type        = list(string)
  default     = ["10.42.0.0/24", "10.42.1.0/24", "10.42.2.0/24"]

  validation {
    condition     = length(var.public_subnet_cidrs) == 3 && alltrue([for cidr in var.public_subnet_cidrs : can(cidrnetmask(cidr))])
    error_message = "public_subnet_cidrs must contain three valid CIDRs."
  }
}

variable "app_subnet_cidrs" {
  description = "Three private ECS subnet CIDRs, ordered like availability_zones."
  type        = list(string)
  default     = ["10.42.16.0/20", "10.42.32.0/20", "10.42.48.0/20"]

  validation {
    condition     = length(var.app_subnet_cidrs) == 3 && alltrue([for cidr in var.app_subnet_cidrs : can(cidrnetmask(cidr))])
    error_message = "app_subnet_cidrs must contain three valid CIDRs."
  }
}

variable "database_subnet_cidrs" {
  description = "Three isolated database subnet CIDRs, ordered like availability_zones."
  type        = list(string)
  default     = ["10.42.64.0/24", "10.42.65.0/24", "10.42.66.0/24"]

  validation {
    condition     = length(var.database_subnet_cidrs) == 3 && alltrue([for cidr in var.database_subnet_cidrs : can(cidrnetmask(cidr))])
    error_message = "database_subnet_cidrs must contain three valid CIDRs."
  }
}

variable "web_domain_name" {
  description = "Public DNS name covered by certificate_arn."
  type        = string

  validation {
    condition     = can(regex("^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.web_domain_name))
    error_message = "web_domain_name must be a lowercase DNS name."
  }
}

variable "route53_zone_id" {
  description = "Existing public Route53 hosted zone ID for web_domain_name."
  type        = string

  validation {
    condition     = can(regex("^Z[A-Z0-9]+$", var.route53_zone_id))
    error_message = "route53_zone_id must be a Route53 hosted zone ID."
  }
}

variable "certificate_arn" {
  description = "Validated ACM certificate ARN for web_domain_name in aws_region."
  type        = string

  validation {
    condition     = can(regex("^arn:aws(?:-[a-z]+)?:acm:[a-z0-9-]+:[0-9]{12}:certificate/[0-9a-f-]+$", var.certificate_arn))
    error_message = "certificate_arn must be an ACM certificate ARN."
  }
}

variable "erpnext_base_url" {
  description = "HTTPS origin of the dedicated production-like ERPNext site."
  type        = string

  validation {
    condition     = can(regex("^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?$", var.erpnext_base_url))
    error_message = "erpnext_base_url must be an HTTPS origin without path, query, fragment, or credentials."
  }
}

variable "erpnext_company" {
  description = "Dedicated ERPNext company for this tenant."
  type        = string

  validation {
    condition     = length(trimspace(var.erpnext_company)) >= 1
    error_message = "erpnext_company is required."
  }
}

variable "erpnext_api_user" {
  description = "Dedicated ERPNext integration user."
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.erpnext_api_user))
    error_message = "erpnext_api_user must be an email-like identifier."
  }
}

variable "erpnext_pos_user" {
  description = "Dedicated ERPNext POS integration user."
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.erpnext_pos_user))
    error_message = "erpnext_pos_user must be an email-like identifier."
  }
}

variable "api_image" {
  description = "Immutable ECR API image URI."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", var.api_image))
    error_message = "api_image must be an immutable ECR image URI with @sha256 digest."
  }
}

variable "migration_image" {
  description = "Immutable ECR migration image URI."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", var.migration_image))
    error_message = "migration_image must be an immutable ECR image URI with @sha256 digest."
  }
}

variable "database_bootstrap_image" {
  description = "Immutable ECR database role-bootstrap image URI."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", var.database_bootstrap_image))
    error_message = "database_bootstrap_image must be an immutable ECR image URI with @sha256 digest."
  }
}

variable "web_image" {
  description = "Immutable ECR web image URI."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{12}\\.dkr\\.ecr\\.[a-z0-9-]+\\.amazonaws\\.com/[a-z0-9._/-]+@sha256:[0-9a-f]{64}$", var.web_image))
    error_message = "web_image must be an immutable ECR image URI with @sha256 digest."
  }
}

variable "postgres_engine_version" {
  description = "Region-supported RDS PostgreSQL 18 minor version pinned before apply."
  type        = string
  default     = "18.3"

  validation {
    condition     = can(regex("^18\\.[0-9]+$", var.postgres_engine_version))
    error_message = "postgres_engine_version must pin a PostgreSQL 18 minor version."
  }
}

variable "db_cluster_instance_class" {
  description = "RDS Multi-AZ DB cluster class supported in all selected AZs."
  type        = string
  default     = "db.m6gd.large"

  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.db_cluster_instance_class))
    error_message = "db_cluster_instance_class must be an RDS instance class."
  }
}

variable "db_allocated_storage_gib" {
  description = "Provisioned io1 storage for each Multi-AZ cluster instance."
  type        = number
  default     = 100

  validation {
    condition     = var.db_allocated_storage_gib >= 100 && floor(var.db_allocated_storage_gib) == var.db_allocated_storage_gib
    error_message = "db_allocated_storage_gib must be an integer of at least 100 GiB."
  }
}

variable "db_iops" {
  description = "Provisioned IOPS; must remain valid for the selected RDS storage size."
  type        = number
  default     = 1000

  validation {
    condition     = var.db_iops >= 1000 && floor(var.db_iops) == var.db_iops
    error_message = "db_iops must be an integer of at least 1000."
  }
}

variable "backup_retention_days" {
  description = "Native RDS automated backup/PITR retention."
  type        = number
  default     = 14

  validation {
    condition     = var.backup_retention_days >= 7 && var.backup_retention_days <= 35 && floor(var.backup_retention_days) == var.backup_retention_days
    error_message = "backup_retention_days must be an integer from 7 through 35."
  }
}

variable "api_desired_count" {
  description = "API replica count after traffic approval."
  type        = number
  default     = 2

  validation {
    condition     = var.api_desired_count >= 2 && floor(var.api_desired_count) == var.api_desired_count
    error_message = "api_desired_count cannot be lower than two."
  }
}

variable "web_desired_count" {
  description = "Web replica count after traffic approval."
  type        = number
  default     = 2

  validation {
    condition     = var.web_desired_count >= 2 && floor(var.web_desired_count) == var.web_desired_count
    error_message = "web_desired_count cannot be lower than two."
  }
}

variable "monitoring_enabled" {
  description = "Enable the scheduled application-role database/recovery monitor after secrets and migrations are ready."
  type        = bool
  default     = false
}

variable "database_connections_alarm_threshold" {
  description = "Connection count treated as application pool saturation evidence."
  type        = number
  default     = 80

  validation {
    condition     = var.database_connections_alarm_threshold >= 20 && floor(var.database_connections_alarm_threshold) == var.database_connections_alarm_threshold
    error_message = "database_connections_alarm_threshold must be an integer of at least 20."
  }
}

variable "alarm_email" {
  description = "Optional operational email subscription; confirmation is external evidence."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.alarm_email == null || can(regex("^[^@[:space:]]+@[^@[:space:]]+$", var.alarm_email))
    error_message = "alarm_email must be null or an email-like identifier."
  }
}

variable "auth_rotation_lambda_arn" {
  description = "Rotation Lambda ARN for the auth HMAC secret, required before traffic."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.auth_rotation_lambda_arn == null || can(regex("^arn:aws(?:-[a-z]+)?:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9-_]+(?::[A-Za-z0-9-_]+)?$", var.auth_rotation_lambda_arn))
    error_message = "auth_rotation_lambda_arn must be null or a Lambda function ARN."
  }
}

variable "state_app_rotation_lambda_arn" {
  description = "Single-user PostgreSQL rotation Lambda ARN for the application role."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.state_app_rotation_lambda_arn == null || can(regex("^arn:aws(?:-[a-z]+)?:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9-_]+(?::[A-Za-z0-9-_]+)?$", var.state_app_rotation_lambda_arn))
    error_message = "state_app_rotation_lambda_arn must be null or a Lambda function ARN."
  }
}

variable "state_migration_rotation_lambda_arn" {
  description = "Single-user PostgreSQL rotation Lambda ARN for the migration role."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.state_migration_rotation_lambda_arn == null || can(regex("^arn:aws(?:-[a-z]+)?:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9-_]+(?::[A-Za-z0-9-_]+)?$", var.state_migration_rotation_lambda_arn))
    error_message = "state_migration_rotation_lambda_arn must be null or a Lambda function ARN."
  }
}

variable "erpnext_rotation_lambda_arn" {
  description = "Rotation Lambda ARN for the dedicated ERPNext API credential."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.erpnext_rotation_lambda_arn == null || can(regex("^arn:aws(?:-[a-z]+)?:lambda:[a-z0-9-]+:[0-9]{12}:function:[A-Za-z0-9-_]+(?::[A-Za-z0-9-_]+)?$", var.erpnext_rotation_lambda_arn))
    error_message = "erpnext_rotation_lambda_arn must be null or a Lambda function ARN."
  }
}

variable "traffic_enabled" {
  description = "Fail-closed latch. Keep false through provisioning, migration, restore, alerts, and smoke."
  type        = bool
  default     = false
}

variable "production_evidence_manifest_sha256" {
  description = "SHA256 of the reviewed Phase 6B.3 production manifest, required to enable traffic."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.production_evidence_manifest_sha256 == null || can(regex("^[0-9a-f]{64}$", var.production_evidence_manifest_sha256))
    error_message = "production_evidence_manifest_sha256 must be null or a lowercase SHA256."
  }
}

variable "traffic_approval_url" {
  description = "Stable HTTPS Notion/change record approving traffic, required to enable traffic."
  type        = string
  default     = null
  nullable    = true

  validation {
    condition     = var.traffic_approval_url == null || can(regex("^https://[^/?#[:space:]]+(?:/[^?#[:space:]]*)?$", var.traffic_approval_url))
    error_message = "traffic_approval_url must be null or a stable HTTPS URL without query or fragment."
  }
}
