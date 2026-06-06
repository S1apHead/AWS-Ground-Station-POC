# =============================================================================
# Module: security
# LLD Ref: LLD-SEC-001
# Description: KMS, GuardDuty, Security Hub, IAM, SCPs, CloudTrail, Config
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

# ── KMS Keys ──────────────────────────────────────────────────────────────────
locals {
  kms_keys = {
    s3_frames        = { description = "SpaceNet S3 raw frames — ITAR controlled", enable_rotation = true }
    kinesis          = { description = "SpaceNet Kinesis streams encryption",       enable_rotation = true }
    dynamodb         = { description = "SpaceNet DynamoDB tables encryption",       enable_rotation = true }
    timestream       = { description = "SpaceNet Timestream database encryption",   enable_rotation = true }
    command_signing  = { description = "SpaceNet TC command signing key",           enable_rotation = false }
    secrets          = { description = "SpaceNet Secrets Manager encryption",       enable_rotation = true }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

resource "aws_kms_key" "keys" {
  for_each                = local.kms_keys
  description             = each.value.description
  deletion_window_in_days = 30
  enable_key_rotation     = each.value.enable_rotation
  multi_region            = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM Root"
        Effect = "Allow"
        Principal = { AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root" }
        Action = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow key administrators"
        Effect = "Allow"
        Principal = { AWS = var.kms_admin_role_arns }
        Action   = ["kms:Create*", "kms:Describe*", "kms:Enable*", "kms:List*",
                    "kms:Put*", "kms:Update*", "kms:Revoke*", "kms:Disable*",
                    "kms:Get*", "kms:Delete*", "kms:ScheduleKeyDeletion", "kms:CancelKeyDeletion"]
        Resource = "*"
      },
      {
        Sid    = "Allow key usage"
        Effect = "Allow"
        Principal = { AWS = var.kms_user_role_arns }
        Action   = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*",
                    "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = "*"
      },
      {
        Sid    = "Allow AWS services"
        Effect = "Allow"
        Principal = {
          Service = ["s3.amazonaws.com", "kinesis.amazonaws.com", "dynamodb.amazonaws.com",
                     "timestream.amazonaws.com", "secretsmanager.amazonaws.com",
                     "lambda.amazonaws.com", "logs.amazonaws.com"]
        }
        Action   = ["kms:Decrypt", "kms:GenerateDataKey*", "kms:DescribeKey"]
        Resource = "*"
      }
    ]
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-kms-${each.key}", KeyPurpose = each.key })
}

resource "aws_kms_alias" "keys" {
  for_each      = local.kms_keys
  name          = "alias/spacenet-${each.key}"
  target_key_id = aws_kms_key.keys[each.key].key_id
}

# ── CloudTrail ────────────────────────────────────────────────────────────────
resource "aws_cloudtrail" "org_trail" {
  name                          = "${var.name_prefix}-org-trail"
  s3_bucket_name                = aws_s3_bucket.cloudtrail.id
  include_global_service_events = true
  is_multi_region_trail         = true
  is_organization_trail         = var.is_org_trail
  enable_log_file_validation    = true
  kms_key_id                    = aws_kms_key.keys["s3_frames"].arn

  event_selector {
    read_write_type           = "All"
    include_management_events = true

    data_resource {
      type   = "AWS::S3::Object"
      values = ["arn:aws:s3:::"]
    }
  }

  cloud_watch_logs_group_arn = "${aws_cloudwatch_log_group.cloudtrail.arn}:*"
  cloud_watch_logs_role_arn  = aws_iam_role.cloudtrail_cw.arn

  tags = merge(var.tags, { Name = "${var.name_prefix}-cloudtrail" })
}

resource "aws_s3_bucket" "cloudtrail" {
  bucket        = "${var.name_prefix}-cloudtrail-${data.aws_caller_identity.current.account_id}"
  force_destroy = false
  tags          = merge(var.tags, { Name = "${var.name_prefix}-cloudtrail" })
}

resource "aws_s3_bucket_public_access_block" "cloudtrail" {
  bucket                  = aws_s3_bucket.cloudtrail.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_policy" "cloudtrail" {
  bucket = aws_s3_bucket.cloudtrail.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AWSCloudTrailAclCheck"
        Effect = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action   = "s3:GetBucketAcl"
        Resource = aws_s3_bucket.cloudtrail.arn
      },
      {
        Sid    = "AWSCloudTrailWrite"
        Effect = "Allow"
        Principal = { Service = "cloudtrail.amazonaws.com" }
        Action   = "s3:PutObject"
        Resource = "${aws_s3_bucket.cloudtrail.arn}/AWSLogs/*"
        Condition = {
          StringEquals = { "s3:x-amz-acl" = "bucket-owner-full-control" }
        }
      }
    ]
  })
}

resource "aws_s3_bucket_object_lock_configuration" "cloudtrail" {
  bucket              = aws_s3_bucket.cloudtrail.id
  object_lock_enabled = "Enabled"
  rule {
    default_retention {
      mode  = "COMPLIANCE"
      years = 7
    }
  }
}

resource "aws_cloudwatch_log_group" "cloudtrail" {
  name              = "/spacenet/cloudtrail"
  retention_in_days = 90
  kms_key_id        = aws_kms_key.keys["s3_frames"].arn
  tags              = var.tags
}

resource "aws_iam_role" "cloudtrail_cw" {
  name = "${var.name_prefix}-cloudtrail-cw-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "cloudtrail.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "cloudtrail_cw" {
  name   = "cloudtrail-cw-policy"
  role   = aws_iam_role.cloudtrail_cw.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
      Resource = "${aws_cloudwatch_log_group.cloudtrail.arn}:*"
    }]
  })
}

# ── GuardDuty ─────────────────────────────────────────────────────────────────
resource "aws_guardduty_detector" "this" {
  enable                       = true
  finding_publishing_frequency = "FIFTEEN_MINUTES"

  datasources {
    s3_logs          { enable = true }
    kubernetes       { audit_logs { enable = true } }
    malware_protection {
      scan_ec2_instance_with_findings {
        ebs_volumes { enable = true }
      }
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-guardduty" })
}

# ── Security Hub ──────────────────────────────────────────────────────────────
resource "aws_securityhub_account" "this" {}

resource "aws_securityhub_standards_subscription" "aws_fsbp" {
  depends_on    = [aws_securityhub_account.this]
  standards_arn = "arn:aws:securityhub:${data.aws_region.current.name}::standards/aws-foundational-security-best-practices/v/1.0.0"
}

resource "aws_securityhub_standards_subscription" "cis" {
  depends_on    = [aws_securityhub_account.this]
  standards_arn = "arn:aws:securityhub:${data.aws_region.current.name}::standards/cis-aws-foundations-benchmark/v/1.4.0"
}

resource "aws_securityhub_standards_subscription" "nist" {
  depends_on    = [aws_securityhub_account.this]
  standards_arn = "arn:aws:securityhub:${data.aws_region.current.name}::standards/nist-800-53/v/5.0.0"
}

# ── SOAR — EventBridge + Lambda ───────────────────────────────────────────────
resource "aws_iam_role" "soar_lambda" {
  name = "${var.name_prefix}-soar-lambda-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "soar_lambda" {
  name   = "soar-policy"
  role   = aws_iam_role.soar_lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["iam:CreateVirtualMFADevice", "iam:DeactivateMFADevice",
                    "iam:DeleteLoginProfile", "iam:UpdateAccessKey"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkAclEntry", "ec2:ModifyInstanceAttribute"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.sns_alert_arns
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:*:*:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["securityhub:UpdateFindings", "securityhub:BatchUpdateFindings"]
        Resource = ["*"]
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "guardduty_high" {
  name        = "${var.name_prefix}-guardduty-high-severity"
  description = "Trigger SOAR on GuardDuty HIGH/CRITICAL findings"

  event_pattern = jsonencode({
    source      = ["aws.guardduty"]
    detail-type = ["GuardDuty Finding"]
    detail      = {
      severity = [{ numeric = [">=", 7.0] }]
    }
  })

  tags = var.tags
}

resource "aws_cloudwatch_event_rule" "root_login" {
  name        = "${var.name_prefix}-root-user-login"
  description = "Alert on root user console login"

  event_pattern = jsonencode({
    source      = ["aws.signin"]
    detail-type = ["AWS Console Sign In via CloudTrail"]
    detail      = {
      userIdentity = { type = ["Root"] }
      eventName    = ["ConsoleLogin"]
    }
  })

  tags = var.tags
}

# ── SNS Alert Topics ──────────────────────────────────────────────────────────
resource "aws_sns_topic" "security_alerts" {
  name              = "${var.name_prefix}-security-alerts"
  kms_master_key_id = aws_kms_key.keys["secrets"].id
  tags              = merge(var.tags, { Name = "${var.name_prefix}-security-alerts" })
}

resource "aws_sns_topic" "noc_alerts" {
  name              = "${var.name_prefix}-noc-alerts"
  kms_master_key_id = aws_kms_key.keys["secrets"].id
  tags              = merge(var.tags, { Name = "${var.name_prefix}-noc-alerts" })
}

# ── AWS Config ────────────────────────────────────────────────────────────────
resource "aws_config_configuration_recorder" "this" {
  name     = "${var.name_prefix}-config-recorder"
  role_arn = aws_iam_role.config.arn

  recording_group {
    all_supported                 = true
    include_global_resource_types = true
  }
}

resource "aws_iam_role" "config" {
  name = "${var.name_prefix}-config-role"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "config.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "config" {
  role       = aws_iam_role.config.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWS_ConfigRole"
}

# ── Secrets Manager (key rotation) ───────────────────────────────────────────
resource "aws_secretsmanager_secret" "iot_endpoint" {
  name                    = "${var.name_prefix}/iot/endpoint"
  kms_key_id              = aws_kms_key.keys["secrets"].id
  recovery_window_in_days = 30
  tags                    = merge(var.tags, { Name = "${var.name_prefix}-iot-endpoint-secret" })
}

resource "aws_secretsmanager_secret" "ground_station_config" {
  name                    = "${var.name_prefix}/ground-station/config"
  kms_key_id              = aws_kms_key.keys["secrets"].id
  recovery_window_in_days = 30
  tags                    = merge(var.tags, { Name = "${var.name_prefix}-gs-config-secret" })
}
