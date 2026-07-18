resource "terraform_data" "runtime_gate" {
  input = {
    enabled         = var.runtime_enabled
    images_ready    = local.runtime_images_ready
    monitoring      = var.monitoring_enabled
    api_replicas    = var.api_desired_count
    web_replicas    = var.web_desired_count
    schema_revision = local.production_revision
  }

  lifecycle {
    precondition {
      condition     = !var.runtime_enabled || local.runtime_ready
      error_message = "Private runtime requires immutable published images and monitoring before two-replica smoke."
    }
  }
}

resource "terraform_data" "traffic_gate" {
  input = {
    enabled                  = var.traffic_enabled
    runtime_enabled          = var.runtime_enabled
    evidence_manifest        = var.production_evidence_manifest_sha256
    approval_url             = var.traffic_approval_url
    evidence_requirements    = local.traffic_evidence_ready
    api_replica_count        = var.api_desired_count
    web_replica_count        = var.web_desired_count
    expected_schema_revision = local.production_revision
  }

  lifecycle {
    precondition {
      condition     = !var.traffic_enabled || local.traffic_evidence_ready
      error_message = "Traffic cannot be enabled before evidence manifest, approval URL and all rotation Lambda ARNs are present."
    }
  }
}

resource "aws_ecs_cluster" "main" {
  name = var.name_prefix

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  configuration {
    execute_command_configuration {
      kms_key_id = aws_kms_key.application.arn
      logging    = "NONE"
    }
  }
}

resource "aws_service_discovery_private_dns_namespace" "main" {
  name        = "myretail.internal"
  description = "Private MyRetail service discovery"
  vpc         = aws_vpc.main.id
}

resource "aws_service_discovery_service" "api" {
  name = "api"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"

    dns_records {
      ttl  = 10
      type = "A"
    }
  }

}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.name_prefix}/api"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.application.arn
}

resource "aws_cloudwatch_log_group" "web" {
  name              = "/ecs/${var.name_prefix}/web"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.application.arn
}

resource "aws_cloudwatch_log_group" "migration" {
  name              = "/ecs/${var.name_prefix}/migration"
  retention_in_days = 365
  kms_key_id        = aws_kms_key.application.arn
}

resource "aws_lb" "web" {
  name                       = substr("${var.name_prefix}-web", 0, 32)
  internal                   = false
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = aws_subnet.public[*].id
  enable_deletion_protection = true
  drop_invalid_header_fields = true
  desync_mitigation_mode     = "strictest"
}

resource "aws_lb_target_group" "web" {
  name        = substr("${var.name_prefix}-web", 0, 32)
  port        = 3000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.main.id

  deregistration_delay = 30

  health_check {
    enabled             = true
    healthy_threshold   = 2
    interval            = 15
    matcher             = "200"
    path                = "/api/health"
    port                = "traffic-port"
    protocol            = "HTTP"
    timeout             = 5
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.web.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.web.arn
  port              = 443
  protocol          = "HTTPS"
  certificate_arn   = var.certificate_arn
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"

  dynamic "default_action" {
    for_each = var.traffic_enabled ? [true] : []

    content {
      type             = "forward"
      target_group_arn = aws_lb_target_group.web.arn
    }
  }

  dynamic "default_action" {
    for_each = var.traffic_enabled ? [] : [true]

    content {
      type = "fixed-response"

      fixed_response {
        content_type = "text/plain"
        message_body = "Production traffic is not approved"
        status_code  = "503"
      }
    }
  }

  depends_on = [terraform_data.traffic_gate]
}

resource "aws_route53_record" "web" {
  zone_id = var.route53_zone_id
  name    = var.web_domain_name
  type    = "A"

  alias {
    name                   = aws_lb.web.dns_name
    zone_id                = aws_lb.web.zone_id
    evaluate_target_health = true
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = data.aws_iam_role.ecs_execution.arn
  task_role_arn            = data.aws_iam_role.api_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name                   = "api"
    image                  = var.api_image
    essential              = true
    readonlyRootFilesystem = true
    user                   = "10001:10001"
    portMappings = [{
      name          = "http"
      containerPort = 8000
      hostPort      = 8000
      protocol      = "tcp"
      appProtocol   = "http"
    }]
    environment = local.common_api_environment
    secrets = [
      { name = "MYRETAIL_AUTH_SECRET", valueFrom = "${aws_secretsmanager_secret.auth.arn}:auth_secret::" },
      { name = "MYRETAIL_AUTH_RATE_LIMIT_SECRET", valueFrom = "${aws_secretsmanager_secret.auth.arn}:auth_rate_limit_secret::" },
      { name = "MYRETAIL_STATE_DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.state_app.arn}:database_url::" },
      { name = "MYRETAIL_ERPNEXT_API_KEY", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:api_key::" },
      { name = "MYRETAIL_ERPNEXT_API_SECRET", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:api_secret::" },
      { name = "MYRETAIL_ERPNEXT_POS_USER_MAP", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:erpnext_pos_user_map::" },
      { name = "MYRETAIL_ERPNEXT_POS_CREDENTIALS_MAP", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:erpnext_pos_credentials_map::" },
      { name = "MYRETAIL_POS_CASHIER_ASSIGNMENTS", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:pos_cashier_assignments::" },
    ]
    healthCheck = {
      command     = ["CMD-SHELL", "python -c \"from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health', timeout=2).read()\""]
      interval    = 15
      timeout     = 3
      retries     = 3
      startPeriod = 20
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.api.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "api"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${var.name_prefix}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = data.aws_iam_role.ecs_execution.arn
  task_role_arn            = data.aws_iam_role.web_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name                   = "web"
    image                  = var.web_image
    essential              = true
    readonlyRootFilesystem = true
    user                   = "10001:10001"
    portMappings = [{
      name          = "http"
      containerPort = 3000
      hostPort      = 3000
      protocol      = "tcp"
      appProtocol   = "http"
    }]
    environment = [
      { name = "NODE_ENV", value = "production" },
      { name = "HOSTNAME", value = "0.0.0.0" },
      { name = "PORT", value = "3000" },
      { name = "MYRETAIL_API_URL", value = local.private_api_base_url },
      { name = "MYRETAIL_WEB_ORIGIN", value = local.web_origin },
    ]
    healthCheck = {
      command     = ["CMD-SHELL", "node -e \"fetch('http://127.0.0.1:3000/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))\""]
      interval    = 15
      timeout     = 3
      retries     = 3
      startPeriod = 20
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.web.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "web"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "migration" {
  family                   = "${var.name_prefix}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = data.aws_iam_role.ecs_execution.arn
  task_role_arn            = data.aws_iam_role.migration_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name                   = "migration"
    image                  = var.migration_image
    essential              = true
    readonlyRootFilesystem = true
    user                   = "10001:10001"
    command                = ["upgrade", local.production_revision]
    environment = [
      { name = "MYRETAIL_ENVIRONMENT", value = "production" },
      { name = "MYRETAIL_STATE_MIGRATION_SSL_MODE", value = "verify-full" },
      { name = "MYRETAIL_STATE_MIGRATION_SSL_ROOT_CERT_PATH", value = local.rds_ca_bundle_path },
    ]
    secrets = [
      { name = "MYRETAIL_STATE_MIGRATION_DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.state_migration.arn}:database_url::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.migration.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "migration"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "database_bootstrap" {
  family                   = "${var.name_prefix}-database-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = data.aws_iam_role.ecs_execution.arn
  task_role_arn            = data.aws_iam_role.migration_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name                   = "database-bootstrap"
    image                  = var.database_bootstrap_image
    essential              = true
    readonlyRootFilesystem = true
    user                   = "999:999"
    environment = [
      { name = "PGDATABASE", value = "myretail_state" },
      { name = "PGSSLMODE", value = "verify-full" },
      { name = "PGSSLROOTCERT", value = local.rds_ca_bundle_path },
    ]
    secrets = [
      { name = "PGHOST", valueFrom = "${one(aws_rds_cluster.state.master_user_secret).secret_arn}:host::" },
      { name = "PGPORT", valueFrom = "${one(aws_rds_cluster.state.master_user_secret).secret_arn}:port::" },
      { name = "PGUSER", valueFrom = "${one(aws_rds_cluster.state.master_user_secret).secret_arn}:username::" },
      { name = "PGPASSWORD", valueFrom = "${one(aws_rds_cluster.state.master_user_secret).secret_arn}:password::" },
      { name = "MYRETAIL_STATE_APP_PASSWORD", valueFrom = "${aws_secretsmanager_secret.state_app.arn}:password::" },
      { name = "MYRETAIL_STATE_MIGRATION_PASSWORD", valueFrom = "${aws_secretsmanager_secret.state_migration.arn}:password::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.migration.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "database-bootstrap"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "preflight" {
  family                   = "${var.name_prefix}-preflight"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = data.aws_iam_role.ecs_execution.arn
  task_role_arn            = data.aws_iam_role.api_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name                   = "preflight"
    image                  = var.api_image
    essential              = true
    readonlyRootFilesystem = true
    user                   = "10001:10001"
    command                = ["myretail-state-preflight"]
    environment            = local.common_api_environment
    secrets = [
      { name = "MYRETAIL_AUTH_SECRET", valueFrom = "${aws_secretsmanager_secret.auth.arn}:auth_secret::" },
      { name = "MYRETAIL_AUTH_RATE_LIMIT_SECRET", valueFrom = "${aws_secretsmanager_secret.auth.arn}:auth_rate_limit_secret::" },
      { name = "MYRETAIL_STATE_DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.state_app.arn}:database_url::" },
      { name = "MYRETAIL_ERPNEXT_API_KEY", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:api_key::" },
      { name = "MYRETAIL_ERPNEXT_API_SECRET", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:api_secret::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.migration.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "preflight"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "monitor" {
  family                   = "${var.name_prefix}-monitor"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = data.aws_iam_role.ecs_execution.arn
  task_role_arn            = data.aws_iam_role.api_task.arn

  runtime_platform {
    cpu_architecture        = "X86_64"
    operating_system_family = "LINUX"
  }

  container_definitions = jsonencode([{
    name                   = "monitor"
    image                  = var.api_image
    essential              = true
    readonlyRootFilesystem = true
    user                   = "10001:10001"
    command                = ["myretail-state-monitor"]
    environment            = local.common_api_environment
    secrets = [
      { name = "MYRETAIL_AUTH_SECRET", valueFrom = "${aws_secretsmanager_secret.auth.arn}:auth_secret::" },
      { name = "MYRETAIL_AUTH_RATE_LIMIT_SECRET", valueFrom = "${aws_secretsmanager_secret.auth.arn}:auth_rate_limit_secret::" },
      { name = "MYRETAIL_STATE_DATABASE_URL", valueFrom = "${aws_secretsmanager_secret.state_app.arn}:database_url::" },
      { name = "MYRETAIL_ERPNEXT_API_KEY", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:api_key::" },
      { name = "MYRETAIL_ERPNEXT_API_SECRET", valueFrom = "${aws_secretsmanager_secret.erpnext.arn}:api_secret::" },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.migration.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "monitor"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name                               = "api"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.api.arn
  desired_count                      = var.runtime_enabled ? var.api_desired_count : 0
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  enable_execute_command             = false
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    assign_public_ip = false
    security_groups  = [aws_security_group.api.id]
    subnets          = aws_subnet.app[*].id
  }

  service_registries {
    registry_arn   = aws_service_discovery_service.api.arn
    container_name = "api"
    container_port = 8000
  }

  depends_on = [terraform_data.runtime_gate]
}

resource "aws_ecs_service" "web" {
  name                               = "web"
  cluster                            = aws_ecs_cluster.main.id
  task_definition                    = aws_ecs_task_definition.web.arn
  desired_count                      = var.runtime_enabled ? var.web_desired_count : 0
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  enable_execute_command             = false
  health_check_grace_period_seconds  = 30
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    assign_public_ip = false
    security_groups  = [aws_security_group.web.id]
    subnets          = aws_subnet.app[*].id
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.web.arn
    container_name   = "web"
    container_port   = 3000
  }

  depends_on = [aws_lb_listener.https, terraform_data.runtime_gate]
}
