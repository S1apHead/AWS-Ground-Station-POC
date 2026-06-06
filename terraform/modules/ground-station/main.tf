# =============================================================================
# Module: ground-station
# LLD Ref: LLD-GS-001
# Description: AWS Ground Station config, dataflow endpoint group, contact profiles
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

# ── Dataflow Endpoint Security Group ─────────────────────────────────────────
resource "aws_security_group" "dataflow_endpoint" {
  name        = "${var.name_prefix}-dataflow-endpoint-sg"
  vpc_id      = var.vpc_id
  description = "AWS Ground Station dataflow endpoint — UDP/55888 inbound only"

  ingress {
    description = "VITA 49 UDP from AWS Ground Station"
    from_port   = 55888
    to_port     = 55888
    protocol    = "udp"
    cidr_blocks = var.ground_station_cidr_blocks
  }

  ingress {
    description = "Health check from VPC"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    description = "Allow all egress (to Kinesis, S3, IoT via VPC endpoints)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-dataflow-endpoint-sg" })
}

# ── Ground Station Dataflow Endpoint Group ────────────────────────────────────
resource "aws_groundstation_dataflow_endpoint_group" "this" {
  endpoint_details {
    endpoint {
      name    = "${var.name_prefix}-dataflow-endpoint"
      address {
        name = var.dataflow_endpoint_ip
        port = 55888
      }
      mtu = 1500
    }
    security_details {
      role_arn           = aws_iam_role.ground_station.arn
      security_group_ids = [aws_security_group.dataflow_endpoint.id]
      subnet_ids         = var.subnet_ids
    }
  }

  contact_pre_pass_duration_seconds  = 300  # T-5 min preparation
  contact_post_pass_duration_seconds = 60

  tags = merge(var.tags, { Name = "${var.name_prefix}-dfeg" })
}

# ── Contact Profile — S-band TT&C ─────────────────────────────────────────────
resource "aws_groundstation_config" "antenna_downlink_ttc" {
  name = "${var.name_prefix}-antenna-downlink-ttc"

  config_data {
    antenna_downlink_demod_decode_config {
      decode_config {
        unvalidated_json = jsonencode({
          type     = "NovatelDecodeConfig"
          settings = {}
        })
      }
      demodulation_config {
        unvalidated_json = jsonencode({
          type = "UnvalidatedJsonDemodConfig"
          settings = {
            centerFrequency = { value = 2250.0, units = "MHz" }
            bandwidth       = { value = 15.0, units = "MHz" }
            polarization    = "RIGHT_HAND"
          }
        })
      }
      spectrum_config {
        bandwidth       { units = "MHz"; value = 15.0 }
        center_frequency { units = "MHz"; value = 2250.0 }
        polarization    = "RIGHT_HAND"
      }
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-antenna-ttc" })
}

# ── Contact Profile — X-band Payload Downlink ─────────────────────────────────
resource "aws_groundstation_config" "antenna_downlink_xband" {
  name = "${var.name_prefix}-antenna-downlink-xband"

  config_data {
    antenna_downlink_config {
      spectrum_config {
        bandwidth        { units = "MHz"; value = 25.0 }
        center_frequency { units = "MHz"; value = 8100.0 }
        polarization     = "RIGHT_HAND"
      }
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-antenna-xband" })
}

# ── Uplink Config — S-band TC ─────────────────────────────────────────────────
resource "aws_groundstation_config" "antenna_uplink" {
  name = "${var.name_prefix}-antenna-uplink"

  config_data {
    antenna_uplink_config {
      spectrum_config {
        center_frequency { units = "MHz"; value = 2025.0 }
        polarization     = "RIGHT_HAND"
      }
      target_eirp { units = "dBW"; value = 20.0 }
      transmit_disabled = false
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-antenna-uplink" })
}

# ── Dataflow — downlink to endpoint ───────────────────────────────────────────
resource "aws_groundstation_config" "dataflow_downlink" {
  name = "${var.name_prefix}-dataflow-downlink"

  config_data {
    dataflow_endpoint_config {
      dataflow_endpoint_name   = "${var.name_prefix}-dataflow-endpoint"
      dataflow_endpoint_region = var.aws_region
    }
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-dataflow-downlink" })
}

# ── Mission Profile ────────────────────────────────────────────────────────────
resource "aws_groundstation_mission_profile" "leo_ttc" {
  name                  = "${var.name_prefix}-leo-ttc-mission-profile"
  contact_pre_pass_duration_seconds  = 300
  contact_post_pass_duration_seconds = 60
  minimum_viable_contact_duration_seconds = 15
  tracking_config_arn   = aws_groundstation_config.antenna_downlink_ttc.arn

  dataflow_edges {
    source      = aws_groundstation_config.antenna_downlink_ttc.arn
    destination = aws_groundstation_config.dataflow_downlink.arn
  }

  dataflow_edges {
    source      = aws_groundstation_config.antenna_uplink.arn
    destination = aws_groundstation_config.dataflow_downlink.arn
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-leo-ttc-profile" })
}

# ── IAM Role for Ground Station ────────────────────────────────────────────────
resource "aws_iam_role" "ground_station" {
  name = "${var.name_prefix}-ground-station-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "groundstation.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })

  tags = var.tags
}

resource "aws_iam_role_policy" "ground_station" {
  name = "ground-station-dataflow-policy"
  role = aws_iam_role.ground_station.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ec2:CreateNetworkInterface", "ec2:DeleteNetworkInterface",
                    "ec2:DescribeNetworkInterfaces"]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["kinesis:PutRecord", "kinesis:PutRecords"]
        Resource = var.kinesis_stream_arns
      }
    ]
  })
}

# ── CloudWatch Alarms ─────────────────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "contact_data_bytes" {
  alarm_name          = "${var.name_prefix}-contact-data-bytes"
  comparison_operator = "LessThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ContactDataBytesReceived"
  namespace           = "SpaceNet/GroundStation"
  period              = 300
  statistic           = "Sum"
  threshold           = 1
  alarm_description   = "No data received during contact window — possible contact failure"
  alarm_actions       = var.sns_alert_arns
  tags                = var.tags
}
