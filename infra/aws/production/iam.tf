data "aws_partition" "current" {}
data "aws_caller_identity" "current" {}

data "aws_iam_role" "ecs_execution" {
  name = "${var.name_prefix}-ecs-execution"
}

data "aws_iam_role" "api_task" {
  name = "${var.name_prefix}-api-task"
}

data "aws_iam_role" "web_task" {
  name = "${var.name_prefix}-web-task"
}

data "aws_iam_role" "migration_task" {
  name = "${var.name_prefix}-migration-task"
}

data "aws_iam_role" "events_ecs" {
  name = "${var.name_prefix}-events-ecs"
}

data "aws_iam_role" "backup" {
  name = "${var.name_prefix}-backup"
}
