# =============================================================================
# Module: ecs-dataflow
# LLD Ref: LLD-GS-001
# Description: ECS Fargate dataflow endpoint container — VITA49 → CCSDS decoder
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
}

# ── ECR Repository ─────────────────────────────────────────────────────────────
resource "aws_ecr_repository" "dataflow_endpoint" {
  name                 = "${var.name_prefix}/dataflow-endpoint"
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = var.kms_key_arn
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-ecr-dataflow" })
}

resource "aws_ecr_lifecycle_policy" "dataflow_endpoint" {
  repository = aws_ecr_repository.dataflow_endpoint.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

# ── ECS Cluster ───────────────────────────────────────────────────────────────
resource "aws_ecs_cluster" "dataflow" {
  name = "${var.name_prefix}-dataflow-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-dataflow-cluster" })
}

resource "aws_ecs_cluster_capacity_providers" "dataflow" {
  cluster_name       = aws_ecs_cluster.dataflow.name
  capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy {
    capacity_provider = "FARGATE"
    weight            = 100
    base              = 1
  }
}

# ── CloudWatch Log Group ──────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "dataflow" {
  name              = "/spacenet/dataflow-endpoint"
  retention_in_days = 30
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

# ── IAM Task Execution Role ───────────────────────────────────────────────────
resource "aws_iam_role" "ecs_execution" {
  name = "${var.name_prefix}-ecs-execution-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "ecs_execution" {
  role       = aws_iam_role.ecs_execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "ecs_execution_secrets" {
  name   = "ecr-secrets-access"
  role   = aws_iam_role.ecs_execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = var.secret_arns
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [var.kms_key_arn]
      }
    ]
  })
}

# ── IAM Task Role (runtime permissions) ───────────────────────────────────────
resource "aws_iam_role" "ecs_task" {
  name = "${var.name_prefix}-ecs-task-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "ecs_task" {
  name   = "dataflow-task-policy"
  role   = aws_iam_role.ecs_task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "KinesisWrite"
        Effect   = "Allow"
        Action   = ["kinesis:PutRecord", "kinesis:PutRecords", "kinesis:DescribeStream"]
        Resource = var.kinesis_stream_arns
      },
      {
        Sid      = "S3RawFrames"
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:PutObjectTagging"]
        Resource = ["${var.s3_raw_frames_arn}/*"]
      },
      {
        Sid      = "IoTPublish"
        Effect   = "Allow"
        Action   = ["iot:Publish", "iot:Connect"]
        Resource = ["arn:aws:iot:${var.aws_region}:*:topic/spacenet/*"]
      },
      {
        Sid      = "CloudWatchMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = "*"
        Condition = {
          StringEquals = { "cloudwatch:namespace" = "SpaceNet/GroundStation" }
        }
      },
      {
        Sid      = "KMSDecrypt"
        Effect   = "Allow"
        Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
        Resource = [var.kms_key_arn]
      }
    ]
  })
}

# ── ECS Task Definition ────────────────────────────────────────────────────────
resource "aws_ecs_task_definition" "dataflow_endpoint" {
  family                   = "${var.name_prefix}-dataflow-endpoint"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "2048"   # 2 vCPU
  memory                   = "4096"   # 4 GB
  execution_role_arn       = aws_iam_role.ecs_execution.arn
  task_role_arn            = aws_iam_role.ecs_task.arn

  container_definitions = jsonencode([
    {
      name      = "dataflow-endpoint"
      image     = "${aws_ecr_repository.dataflow_endpoint.repository_url}:latest"
      essential = true

      portMappings = [
        { containerPort = 55888, protocol = "udp", name = "vita49-udp" },
        { containerPort = 8080,  protocol = "tcp", name = "health"     }
      ]

      environment = [
        { name = "UDP_PORT",             value = "55888" },
        { name = "KINESIS_HK_STREAM",    value = var.kinesis_hk_stream_name },
        { name = "KINESIS_RAW_STREAM",   value = var.kinesis_raw_stream_name },
        { name = "S3_RAW_FRAMES_BUCKET", value = var.s3_raw_frames_bucket },
        { name = "AWS_REGION",           value = var.aws_region },
        { name = "LOG_LEVEL",            value = "INFO" },
        { name = "BUFFER_SIZE",          value = "65536" },
        { name = "WORKER_THREADS",       value = "4" }
      ]

      secrets = [
        { name = "IOT_ENDPOINT", valueFrom = "${var.iot_endpoint_secret_arn}" }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options   = {
          "awslogs-group"         = aws_cloudwatch_log_group.dataflow.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = "dataflow"
        }
      }

      healthCheck = {
        command     = ["CMD-SHELL", "curl -f http://localhost:8080/health || exit 1"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }

      ulimits = [
        { name = "nofile", softLimit = 65536, hardLimit = 65536 }
      ]

      readonlyRootFilesystem = true
      user                   = "1000:1000"
    }
  ])

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-dataflow-task-def" })
}

# ── ECS Security Group ────────────────────────────────────────────────────────
resource "aws_security_group" "ecs_task" {
  name        = "${var.name_prefix}-ecs-task-sg"
  vpc_id      = var.vpc_id
  description = "ECS Fargate dataflow endpoint task SG"

  ingress {
    description = "VITA 49 UDP from Ground Station"
    from_port   = 55888
    to_port     = 55888
    protocol    = "udp"
    cidr_blocks = var.ground_station_cidr_blocks
  }

  ingress {
    description = "Health check from ALB / VPC"
    from_port   = 8080
    to_port     = 8080
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }

  egress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
    description = "HTTPS to AWS services via VPC endpoints"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-ecs-task-sg" })
}

# ── ECS Service ───────────────────────────────────────────────────────────────
resource "aws_ecs_service" "dataflow_endpoint" {
  name                               = "${var.name_prefix}-dataflow-endpoint"
  cluster                            = aws_ecs_cluster.dataflow.id
  task_definition                    = aws_ecs_task_definition.dataflow_endpoint.arn
  desired_count                      = var.desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "LATEST"
  health_check_grace_period_seconds  = 60
  enable_execute_command             = false  # Disabled in production

  network_configuration {
    subnets          = var.subnet_ids
    security_groups  = [aws_security_group.ecs_task.id]
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  deployment_controller {
    type = "ECS"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-dataflow-service" })

  lifecycle {
    ignore_changes = [desired_count]  # Managed by autoscaling
  }
}

# ── Auto Scaling ──────────────────────────────────────────────────────────────
resource "aws_appautoscaling_target" "dataflow" {
  max_capacity       = 10
  min_capacity       = var.desired_count
  resource_id        = "service/${aws_ecs_cluster.dataflow.name}/${aws_ecs_service.dataflow_endpoint.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "dataflow_cpu" {
  name               = "${var.name_prefix}-dataflow-cpu-scaling"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.dataflow.resource_id
  scalable_dimension = aws_appautoscaling_target.dataflow.scalable_dimension
  service_namespace  = aws_appautoscaling_target.dataflow.service_namespace

  target_tracking_scaling_policy_configuration {
    target_value       = 60.0
    scale_in_cooldown  = 300
    scale_out_cooldown = 60

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# ── CloudWatch Alarms ─────────────────────────────────────────────────────────
resource "aws_cloudwatch_metric_alarm" "ecs_cpu_high" {
  alarm_name          = "${var.name_prefix}-dataflow-cpu-high"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "CPUUtilization"
  namespace           = "AWS/ECS"
  period              = 60
  statistic           = "Average"
  threshold           = 80.0
  alarm_actions       = var.sns_alert_arns
  dimensions = {
    ClusterName = aws_ecs_cluster.dataflow.name
    ServiceName = aws_ecs_service.dataflow_endpoint.name
  }
  tags = var.tags
}
