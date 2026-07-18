mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
      arn        = "arn:aws:iam::111122223333:user/terraform-test"
      user_id    = "terraform-test"
    }
  }

  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }

  mock_data "aws_iam_policy_document" {
    defaults = {
      json = jsonencode({
        Version   = "2012-10-17"
        Statement = []
      })
    }
  }
}

variables {
  aws_region        = "eu-central-1"
  state_bucket_name = "myretail-production-state-111122223333"
}

run "deployment_role_is_separate_from_preprovisioned_runtime_roles" {
  command = plan

  assert {
    condition     = aws_iam_role.github_production.name == "myretail-deployment-github-oidc"
    error_message = "The GitHub deployment role must remain outside the production runtime role prefix."
  }

  assert {
    condition = toset(values(local.production_role_names)) == toset([
      "myretail-production-api-task",
      "myretail-production-backup",
      "myretail-production-ecs-execution",
      "myretail-production-events-ecs",
      "myretail-production-migration-task",
      "myretail-production-web-task",
    ])
    error_message = "Production must consume only the six fixed bootstrap IAM roles."
  }
}
