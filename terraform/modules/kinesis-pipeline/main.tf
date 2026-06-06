# =============================================================================
# Module: kinesis-pipeline
# LLD Ref: LLD-DP-001
# Description: Kinesis Data Streams, Firehose → S3, Lambda consumers
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

locals {
  streams = {
    hk      = { shards = 10, retention_hours = 24,  name = "${var.name_prefix}-telemetry-hk" }
    science = { shards = 5,  retention_hours = 168, name = "${var.name_prefix}-telemetry-science" }
    events  = { shards = 4,  retention_hours = 24,  name = "${var.name_prefix}-telemetry-events" }
    raw     = { shards = 8,  retention_hours = 24,  name = "${var.name_prefix}-raw-frames" }
  }
}

# ── Kinesis Data Streams ──────────────────────────────────────────────────────
resource "aws_kinesis_stream" "streams" {
  for_each         = local.streams
  name             = each.value.name
  shard_count      = each.value.shards
  retention_period = each.value.retention_hours

  encryption_type = "KMS"
  kms_key_id      = var.kms_key_id

  stream_mode_details {
    stream_mode = "PROVISIONED"
  }

  tags = merge(var.tags, { Name = each.value.name, StreamType = each.key })
}

# ── Kinesis Firehose → S3 (raw frames archive) ────────────────────────────────
resource "aws_iam_role" "firehose" {
  name = "${var.name_prefix}-firehose-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "firehose.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "firehose" {
  name   = "firehose-policy"
  role   = aws_iam_role.firehose.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kinesis:GetRecords", "kinesis:GetShardIterator",
                    "kinesis:DescribeStream", "kinesis:ListShards"]
        Resource = [aws_kinesis_stream.streams["raw"].arn]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:PutObjectAcl", "s3:GetBucketLocation"]
        Resource = ["${var.s3_raw_frames_arn}", "${var.s3_raw_frames_arn}/*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = [var.kms_key_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:PutLogEvents"]
        Resource = ["*"]
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "firehose" {
  name              = "/spacenet/firehose"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_cloudwatch_log_stream" "firehose_s3" {
  name           = "s3-delivery"
  log_group_name = aws_cloudwatch_log_group.firehose.name
}

resource "aws_kinesis_firehose_delivery_stream" "raw_frames" {
  name        = "${var.name_prefix}-raw-frames-firehose"
  destination = "extended_s3"

  kinesis_source_configuration {
    kinesis_stream_arn = aws_kinesis_stream.streams["raw"].arn
    role_arn           = aws_iam_role.firehose.arn
  }

  extended_s3_configuration {
    role_arn            = aws_iam_role.firehose.arn
    bucket_arn          = var.s3_raw_frames_arn
    buffering_size      = 128
    buffering_interval  = 300
    compression_format  = "UNCOMPRESSED"
    kms_key_arn         = var.kms_key_arn

    prefix              = "year=!{timestamp:yyyy}/month=!{timestamp:MM}/day=!{timestamp:dd}/"
    error_output_prefix = "errors/year=!{timestamp:yyyy}/month=!{timestamp:MM}/!{firehose:error-output-type}/"

    cloudwatch_logging_options {
      enabled         = true
      log_group_name  = aws_cloudwatch_log_group.firehose.name
      log_stream_name = aws_cloudwatch_log_stream.firehose_s3.name
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-raw-frames-firehose" })
}

# ── Lambda: HK Telemetry Consumer → Timestream ────────────────────────────────
resource "aws_iam_role" "lambda_hk_consumer" {
  name = "${var.name_prefix}-lambda-hk-consumer-role"

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

resource "aws_iam_role_policy" "lambda_hk_consumer" {
  name   = "hk-consumer-policy"
  role   = aws_iam_role.lambda_hk_consumer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["kinesis:GetRecords", "kinesis:GetShardIterator",
                  "kinesis:DescribeStream", "kinesis:ListShards",
                  "kinesis:ListStreams"]
        Resource = [aws_kinesis_stream.streams["hk"].arn]
      },
      {
        Effect   = "Allow"
        Action   = ["timestream:WriteRecords", "timestream:DescribeEndpoints"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["arn:aws:logs:*:*:*"]
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.kms_key_arn]
      },
      {
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = ["*"]
      }
    ]
  })
}

resource "aws_cloudwatch_log_group" "lambda_hk" {
  name              = "/aws/lambda/${var.name_prefix}-hk-consumer"
  retention_in_days = 30
  tags              = var.tags
}

resource "aws_lambda_function" "hk_consumer" {
  function_name = "${var.name_prefix}-hk-consumer"
  role          = aws_iam_role.lambda_hk_consumer.arn
  package_type  = "Image"
  image_uri     = "${var.ecr_base_url}/${var.name_prefix}/hk-consumer:latest"
  timeout       = 300
  memory_size   = 1024

  environment {
    variables = {
      TIMESTREAM_DATABASE = var.timestream_database_name
      TIMESTREAM_TABLE    = var.timestream_hk_table_name
      AWS_REGION_NAME     = var.aws_region
      LOG_LEVEL           = "INFO"
    }
  }

  tracing_config {
    mode = "Active"
  }

  vpc_config {
    subnet_ids         = var.lambda_subnet_ids
    security_group_ids = [aws_security_group.lambda.id]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-hk-consumer" })
}

resource "aws_security_group" "lambda" {
  name        = "${var.name_prefix}-lambda-sg"
  vpc_id      = var.vpc_id
  description = "Lambda consumer security group"
  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  tags = merge(var.tags, { Name = "${var.name_prefix}-lambda-sg" })
}

resource "aws_lambda_event_source_mapping" "hk_kinesis" {
  event_source_arn              = aws_kinesis_stream.streams["hk"].arn
  function_name                 = aws_lambda_function.hk_consumer.arn
  starting_position             = "LATEST"
  batch_size                    = 500
  parallelization_factor        = 5
  bisect_batch_on_function_error = true
  maximum_retry_attempts        = 3

  destination_config {
    on_failure {
      destination_arn = var.sqs_dlq_arn
    }
  }
}

# ── CloudWatch Alarms ─────────────────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "kinesis_iterator_age" {
  for_each            = local.streams
  alarm_name          = "${each.value.name}-iterator-age-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "GetRecords.IteratorAgeMilliseconds"
  namespace           = "AWS/Kinesis"
  period              = 60
  statistic           = "Maximum"
  threshold           = 60000  # 60 seconds lag
  alarm_description   = "Kinesis consumer is falling behind — ${each.value.name}"
  alarm_actions       = var.sns_alert_arns
  dimensions = { StreamName = each.value.name }
  tags = var.tags
}
