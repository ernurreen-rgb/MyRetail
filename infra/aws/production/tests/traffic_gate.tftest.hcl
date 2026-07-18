mock_provider "aws" {
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = jsonencode({
        Version   = "2012-10-17"
        Statement = []
      })
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
      arn        = "arn:aws:iam::111122223333:user/terraform-test"
      user_id    = "terraform-test"
    }
  }

  mock_data "aws_iam_role" {
    defaults = {
      arn  = "arn:aws:iam::111122223333:role/myretail-production-test"
      id   = "myretail-production-test"
      name = "myretail-production-test"
    }
  }
}

variables {
  aws_region         = "eu-central-1"
  availability_zones = ["eu-central-1a", "eu-central-1b", "eu-central-1c"]

  tenant_id   = "10000000-0000-4000-8000-000000000001"
  tenant_slug = "acceptance"

  web_domain_name = "app.example.com"
  route53_zone_id = "ZTEST123"
  certificate_arn = "arn:aws:acm:eu-central-1:111122223333:certificate/10000000-0000-4000-8000-000000000001"

  erpnext_base_url = "https://erp.example.com"
  erpnext_company  = "Acceptance"
  erpnext_api_user = "api@example.com"
  erpnext_pos_user = "pos@example.com"

  api_image                = "111122223333.dkr.ecr.eu-central-1.amazonaws.com/myretail-production/api@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  database_bootstrap_image = "111122223333.dkr.ecr.eu-central-1.amazonaws.com/myretail-production/database-bootstrap@sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
  migration_image          = "111122223333.dkr.ecr.eu-central-1.amazonaws.com/myretail-production/migration@sha256:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
  web_image                = "111122223333.dkr.ecr.eu-central-1.amazonaws.com/myretail-production/web@sha256:dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
}

override_resource {
  target = aws_rds_cluster.state
  values = {
    endpoint = "database.example.internal"
    master_user_secret = [{
      kms_key_id    = "arn:aws:kms:eu-central-1:111122223333:key/10000000-0000-4000-8000-000000000001"
      secret_arn    = "arn:aws:secretsmanager:eu-central-1:111122223333:secret:rds-master"
      secret_status = "active"
    }]
  }
}

run "traffic_stays_closed_by_default" {
  command = plan

  assert {
    condition     = aws_ecs_service.api.desired_count == 0
    error_message = "API tasks must remain at zero before private runtime approval."
  }

  assert {
    condition     = aws_ecs_service.web.desired_count == 0
    error_message = "Web tasks must remain at zero before private runtime approval."
  }

  assert {
    condition     = aws_lb_listener.https.default_action[0].type == "fixed-response"
    error_message = "Public HTTPS must fail closed before traffic approval."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.state_monitor.state == "DISABLED"
    error_message = "The scheduled monitor must wait until runtime secrets and migrations are ready."
  }
}

run "private_runtime_supports_two_replica_smoke_without_public_traffic" {
  command = plan

  variables {
    monitoring_enabled = true
    runtime_enabled    = true
  }

  assert {
    condition     = aws_ecs_service.api.desired_count == 2
    error_message = "Private smoke must run the approved API replica floor."
  }

  assert {
    condition     = aws_ecs_service.web.desired_count == 2
    error_message = "Private smoke must run the approved web replica floor."
  }

  assert {
    condition     = aws_lb_listener.https.default_action[0].type == "fixed-response"
    error_message = "Starting private runtime must not open public HTTPS."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.api_running_tasks.actions_enabled
    error_message = "Runtime alert delivery must be testable before public traffic."
  }
}

run "traffic_rejects_missing_evidence" {
  command = plan

  variables {
    traffic_enabled = true
  }

  expect_failures = [terraform_data.traffic_gate]
}

run "traffic_opens_only_with_complete_evidence" {
  command = plan

  variables {
    monitoring_enabled                  = true
    runtime_enabled                     = true
    traffic_enabled                     = true
    production_evidence_manifest_sha256 = "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
    traffic_approval_url                = "https://app.notion.com/approved-cutover"
    auth_rotation_lambda_arn            = "arn:aws:lambda:eu-central-1:111122223333:function:auth-rotation"
    state_app_rotation_lambda_arn       = "arn:aws:lambda:eu-central-1:111122223333:function:state-app-rotation"
    state_migration_rotation_lambda_arn = "arn:aws:lambda:eu-central-1:111122223333:function:state-migration-rotation"
    erpnext_rotation_lambda_arn         = "arn:aws:lambda:eu-central-1:111122223333:function:erpnext-rotation"
  }

  assert {
    condition     = aws_ecs_service.api.desired_count == 2
    error_message = "A complete evidence gate must enable the approved API replica floor."
  }

  assert {
    condition     = aws_ecs_service.web.desired_count == 2
    error_message = "A complete evidence gate must enable the approved web replica floor."
  }

  assert {
    condition     = aws_cloudwatch_event_rule.state_monitor.state == "ENABLED"
    error_message = "The scheduled monitor must be enabled before traffic."
  }

  assert {
    condition     = aws_lb_listener.https.default_action[0].type == "forward"
    error_message = "Approved traffic must forward only after the complete evidence gate."
  }
}
