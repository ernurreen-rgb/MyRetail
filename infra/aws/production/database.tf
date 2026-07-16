resource "aws_db_subnet_group" "main" {
  name       = "${var.name_prefix}-database"
  subnet_ids = aws_subnet.database[*].id

  tags = {
    Name = "${var.name_prefix}-database"
  }
}

resource "aws_rds_cluster_parameter_group" "postgres" {
  name        = "${var.name_prefix}-postgres18"
  family      = "postgres18"
  description = "MyRetail PostgreSQL 18 security and audit baseline"

  parameter {
    name         = "rds.force_ssl"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "log_connections"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "log_disconnections"
    value        = "1"
    apply_method = "pending-reboot"
  }

  parameter {
    name         = "log_lock_waits"
    value        = "1"
    apply_method = "pending-reboot"
  }
}

resource "aws_rds_cluster" "state" {
  cluster_identifier = "${var.name_prefix}-state"
  database_name      = "myretail_state"
  master_username    = "myretail_cluster_admin"

  engine                          = "postgres"
  engine_version                  = var.postgres_engine_version
  db_cluster_instance_class       = var.db_cluster_instance_class
  allocated_storage               = var.db_allocated_storage_gib
  storage_type                    = "io1"
  iops                            = var.db_iops
  availability_zones              = var.availability_zones
  db_subnet_group_name            = aws_db_subnet_group.main.name
  vpc_security_group_ids          = [aws_security_group.database.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.postgres.name

  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.application.arn
  storage_encrypted             = true
  kms_key_id                    = aws_kms_key.application.arn

  backup_retention_period      = var.backup_retention_days
  preferred_backup_window      = "01:00-02:00"
  preferred_maintenance_window = "sun:03:00-sun:04:00"
  copy_tags_to_snapshot        = true
  deletion_protection          = true
  skip_final_snapshot          = false
  final_snapshot_identifier    = "${var.name_prefix}-state-final"

  enabled_cloudwatch_logs_exports = ["postgresql"]
  apply_immediately               = false

  lifecycle {
    prevent_destroy = true
  }

  depends_on = [aws_cloudwatch_log_group.rds_postgresql]
}
