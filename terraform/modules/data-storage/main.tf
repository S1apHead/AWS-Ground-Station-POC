# =============================================================================
# Module: data-storage
# LLD Ref: LLD-DP-001
# Description: S3 buckets, DynamoDB tables, Timestream, OpenSearch, Aurora
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
    random = { source = "hashicorp/random", version = "~> 3.0" }
  }
}

resource "random_id" "bucket_suffix" {
  byte_length = 4
}

# ── S3 Buckets ────────────────────────────────────────────────────────────────
locals {
  buckets = {
    raw_frames       = { name = "${var.name_prefix}-raw-frames-${random_id.bucket_suffix.hex}",       worm = true,  worm_years = 7,  worm_mode = "COMPLIANCE" }
    contact_reports  = { name = "${var.name_prefix}-contact-reports-${random_id.bucket_suffix.hex}",  worm = true,  worm_years = 5,  worm_mode = "GOVERNANCE" }
    science_data     = { name = "${var.name_prefix}-science-data-${random_id.bucket_suffix.hex}",     worm = true,  worm_years = 10, worm_mode = "GOVERNANCE" }
    artifacts        = { name = "${var.name_prefix}-artifacts-${random_id.bucket_suffix.hex}",        worm = false, worm_years = 0,  worm_mode = "" }
    logs_archive     = { name = "${var.name_prefix}-logs-archive-${random_id.bucket_suffix.hex}",     worm = true,  worm_years = 7,  worm_mode = "COMPLIANCE" }
  }
}

resource "aws_s3_bucket" "buckets" {
  for_each      = local.buckets
  bucket        = each.value.name
  force_destroy = var.environment != "prod"
  tags          = merge(var.tags, { Name = each.value.name, BucketType = each.key })
}

resource "aws_s3_bucket_versioning" "buckets" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.buckets[each.key].id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "buckets" {
  for_each = local.buckets
  bucket   = aws_s3_bucket.buckets[each.key].id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "buckets" {
  for_each                = local.buckets
  bucket                  = aws_s3_bucket.buckets[each.key].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_object_lock_configuration" "worm_buckets" {
  for_each            = { for k, v in local.buckets : k => v if v.worm }
  bucket              = aws_s3_bucket.buckets[each.key].id
  object_lock_enabled = "Enabled"

  rule {
    default_retention {
      mode  = each.value.worm_mode
      years = each.value.worm_years
    }
  }
}

# Lifecycle rules for raw_frames
resource "aws_s3_bucket_lifecycle_configuration" "raw_frames" {
  bucket = aws_s3_bucket.buckets["raw_frames"].id
  rule {
    id     = "tiered-storage"
    status = "Enabled"
    transition { days = 30;  storage_class = "STANDARD_IA" }
    transition { days = 90;  storage_class = "GLACIER_IR" }
    transition { days = 365; storage_class = "DEEP_ARCHIVE" }
  }
}

# ── DynamoDB Tables ───────────────────────────────────────────────────────────
resource "aws_dynamodb_table" "satellites" {
  name             = "${var.name_prefix}-satellites"
  billing_mode     = "PAY_PER_REQUEST"
  hash_key         = "satellite_id"
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  attribute { name = "satellite_id"; type = "S" }
  attribute { name = "state";        type = "S" }

  global_secondary_index {
    name            = "state-index"
    hash_key        = "state"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }

  server_side_encryption {
    enabled     = true
    kms_key_arn = var.kms_key_arn
  }

  ttl { attribute_name = "ttl"; enabled = false }

  replica { region_name = var.dr_region }

  tags = merge(var.tags, { Name = "${var.name_prefix}-satellites" })
}

resource "aws_dynamodb_table" "contacts" {
  name         = "${var.name_prefix}-contacts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "contact_id"

  attribute { name = "contact_id";  type = "S" }
  attribute { name = "satellite_id"; type = "S" }
  attribute { name = "status";       type = "S" }
  attribute { name = "start_time";   type = "S" }

  global_secondary_index {
    name            = "satellite-index"
    hash_key        = "satellite_id"
    range_key       = "start_time"
    projection_type = "ALL"
  }

  global_secondary_index {
    name            = "status-index"
    hash_key        = "status"
    range_key       = "start_time"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true; kms_key_arn = var.kms_key_arn }
  replica { region_name = var.dr_region }

  tags = merge(var.tags, { Name = "${var.name_prefix}-contacts" })
}

resource "aws_dynamodb_table" "telemetry_state" {
  name         = "${var.name_prefix}-telemetry-state"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "satellite_id"
  range_key    = "param"

  attribute { name = "satellite_id"; type = "S" }
  attribute { name = "param";        type = "S" }

  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true; kms_key_arn = var.kms_key_arn }
  replica { region_name = var.dr_region }

  tags = merge(var.tags, { Name = "${var.name_prefix}-telemetry-state" })
}

resource "aws_dynamodb_table" "commands" {
  name         = "${var.name_prefix}-commands"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "command_id"

  attribute { name = "command_id";   type = "S" }
  attribute { name = "satellite_id"; type = "S" }
  attribute { name = "status";       type = "S" }

  global_secondary_index {
    name            = "satellite-cmd-index"
    hash_key        = "satellite_id"
    range_key       = "status"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true; kms_key_arn = var.kms_key_arn }
  replica { region_name = var.dr_region }

  tags = merge(var.tags, { Name = "${var.name_prefix}-commands" })
}

resource "aws_dynamodb_table" "anomalies" {
  name         = "${var.name_prefix}-anomalies"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "alert_id"

  attribute { name = "alert_id";     type = "S" }
  attribute { name = "satellite_id"; type = "S" }
  attribute { name = "severity";     type = "S" }

  global_secondary_index {
    name            = "satellite-alert-index"
    hash_key        = "satellite_id"
    range_key       = "severity"
    projection_type = "ALL"
  }

  point_in_time_recovery { enabled = true }
  server_side_encryption { enabled = true; kms_key_arn = var.kms_key_arn }
  replica { region_name = var.dr_region }

  tags = merge(var.tags, { Name = "${var.name_prefix}-anomalies" })
}

# ── Amazon Timestream ─────────────────────────────────────────────────────────
resource "aws_timestreamwrite_database" "telemetry" {
  database_name = "${var.name_prefix}-telemetry"
  kms_key_id    = var.kms_key_arn
  tags          = merge(var.tags, { Name = "${var.name_prefix}-timestream-db" })
}

resource "aws_timestreamwrite_table" "satellite_hk" {
  database_name = aws_timestreamwrite_database.telemetry.database_name
  table_name    = "satellite_hk"

  retention_properties {
    memory_store_retention_period_in_hours  = 24
    magnetic_store_retention_period_in_days = 396  # 13 months
  }

  magnetic_store_write_properties {
    enable_magnetic_store_writes = true
    magnetic_store_rejected_data_location {
      s3_configuration {
        bucket_name        = aws_s3_bucket.buckets["artifacts"].id
        object_key_prefix  = "timestream-rejected/"
        kms_key_id         = var.kms_key_arn
        encryption_option  = "SSE_KMS"
      }
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-ts-satellite-hk" })
}

resource "aws_timestreamwrite_table" "orbital_state" {
  database_name = aws_timestreamwrite_database.telemetry.database_name
  table_name    = "orbital_state"

  retention_properties {
    memory_store_retention_period_in_hours  = 24
    magnetic_store_retention_period_in_days = 396
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-ts-orbital" })
}

# ── SQS Dead Letter Queue ─────────────────────────────────────────────────────
resource "aws_sqs_queue" "dlq" {
  name                      = "${var.name_prefix}-lambda-dlq"
  message_retention_seconds = 1209600  # 14 days
  kms_master_key_id         = var.kms_key_id
  tags                      = merge(var.tags, { Name = "${var.name_prefix}-lambda-dlq" })
}
