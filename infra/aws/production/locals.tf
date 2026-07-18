locals {
  web_origin           = "https://${var.web_domain_name}"
  private_api_base_url = "http://api.myretail.internal:8000"
  rds_ca_bundle_path   = "/etc/ssl/certs/aws-rds-global-bundle.pem"
  production_revision  = "20260718_06"
  placeholder_digest   = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
  runtime_images_ready = alltrue([
    for image in [
      var.api_image,
      var.database_bootstrap_image,
      var.migration_image,
      var.web_image,
    ] : !endswith(image, local.placeholder_digest)
  ])
  runtime_ready = var.monitoring_enabled && local.runtime_images_ready
  traffic_evidence_ready = (
    var.runtime_enabled &&
    var.monitoring_enabled &&
    try(length(var.production_evidence_manifest_sha256) == 64, false) &&
    try(startswith(var.traffic_approval_url, "https://"), false) &&
    try(length(var.auth_rotation_lambda_arn) > 0, false) &&
    try(length(var.state_app_rotation_lambda_arn) > 0, false) &&
    try(length(var.state_migration_rotation_lambda_arn) > 0, false) &&
    try(length(var.erpnext_rotation_lambda_arn) > 0, false)
  )

  common_api_environment = [
    { name = "MYRETAIL_ENVIRONMENT", value = "production" },
    { name = "MYRETAIL_LOG_LEVEL", value = "INFO" },
    { name = "MYRETAIL_TENANCY_MODE", value = "isolated_site" },
    { name = "MYRETAIL_TENANT_ID", value = var.tenant_id },
    { name = "MYRETAIL_TENANT_SLUG", value = var.tenant_slug },
    { name = "MYRETAIL_TENANT_ROUTE_VERSION", value = tostring(var.tenant_route_version) },
    { name = "MYRETAIL_AUTH_ISSUER", value = "myretail-api" },
    { name = "MYRETAIL_AUTH_AUDIENCE", value = "myretail" },
    { name = "MYRETAIL_AUTH_CLIENT_IP_MODE", value = "trusted_proxy" },
    { name = "MYRETAIL_AUTH_TRUSTED_PROXY_CIDRS", value = jsonencode(var.app_subnet_cidrs) },
    { name = "MYRETAIL_ERPNEXT_BASE_URL", value = var.erpnext_base_url },
    { name = "MYRETAIL_ERPNEXT_COMPANY", value = var.erpnext_company },
    { name = "MYRETAIL_ERPNEXT_API_USER", value = var.erpnext_api_user },
    { name = "MYRETAIL_ERPNEXT_POS_USER", value = var.erpnext_pos_user },
    { name = "MYRETAIL_STATE_BACKEND", value = "postgresql" },
    { name = "MYRETAIL_STATE_PRODUCTION_ENABLEMENT", value = "controlled" },
    { name = "MYRETAIL_STATE_POOL_MIN_SIZE", value = "2" },
    { name = "MYRETAIL_STATE_POOL_MAX_SIZE", value = "10" },
    { name = "MYRETAIL_STATE_POOL_ACQUIRE_TIMEOUT_SECONDS", value = "5" },
    { name = "MYRETAIL_STATE_STATEMENT_TIMEOUT_MS", value = "5000" },
    { name = "MYRETAIL_STATE_LOCK_TIMEOUT_MS", value = "2000" },
    { name = "MYRETAIL_STATE_RECOVERY_MAX_AGE_SECONDS", value = "900" },
    { name = "MYRETAIL_STATE_POSTGRES_SSL_MODE", value = "verify-full" },
    { name = "MYRETAIL_STATE_POSTGRES_SSL_ROOT_CERT_PATH", value = local.rds_ca_bundle_path },
  ]
}
