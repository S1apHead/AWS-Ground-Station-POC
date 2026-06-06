# =============================================================================
# Module: eks-fleet
# LLD Ref: LLD-FM-001 Fleet Management
# Description: EKS private cluster + managed node groups + IRSA + App Mesh
#              + ADOT Collector + External Secrets + Flux CD bootstrap
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws        = { source = "hashicorp/aws",        version = "~> 5.0" }
    kubernetes = { source = "hashicorp/kubernetes",  version = "~> 2.27" }
    helm       = { source = "hashicorp/helm",        version = "~> 2.13" }
    tls        = { source = "hashicorp/tls",         version = "~> 4.0" }
  }
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# ── IAM — Cluster Role ────────────────────────────────────────────────────────
resource "aws_iam_role" "cluster" {
  name = "${var.name_prefix}-eks-cluster-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "cluster_policies" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy",
    "arn:aws:iam::aws:policy/AmazonEKSVPCResourceController",
  ])
  role       = aws_iam_role.cluster.name
  policy_arn = each.value
}

# ── CloudWatch Log Group ──────────────────────────────────────────────────────
resource "aws_cloudwatch_log_group" "eks" {
  name              = "/aws/eks/${var.name_prefix}-fleet/cluster"
  retention_in_days = 90
  kms_key_id        = var.kms_key_arn
  tags              = var.tags
}

# ── EKS Cluster ───────────────────────────────────────────────────────────────
resource "aws_eks_cluster" "this" {
  name     = "${var.name_prefix}-fleet"
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = var.private_subnet_ids
    endpoint_public_access  = false
    endpoint_private_access = true
    security_group_ids      = [aws_security_group.cluster.id]
  }

  enabled_cluster_log_types = [
    "api", "audit", "authenticator", "controllerManager", "scheduler"
  ]

  encryption_config {
    provider   { key_arn = var.kms_key_arn }
    resources  = ["secrets"]
  }

  kubernetes_network_config {
    service_cidr       = "172.20.0.0/16"
    ip_family          = "ipv4"
  }

  access_config {
    authentication_mode                         = "API_AND_CONFIG_MAP"
    bootstrap_cluster_creator_admin_permissions = false
  }

  depends_on = [
    aws_iam_role_policy_attachment.cluster_policies,
    aws_cloudwatch_log_group.eks,
  ]

  tags = merge(var.tags, { Name = "${var.name_prefix}-fleet-cluster" })
}

# ── Cluster Security Group ────────────────────────────────────────────────────
resource "aws_security_group" "cluster" {
  name        = "${var.name_prefix}-eks-cluster-sg"
  description = "EKS cluster control plane — SpaceNet Fleet Management"
  vpc_id      = var.vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-eks-cluster-sg" })
}

resource "aws_security_group_rule" "cluster_ingress_nodes" {
  type                     = "ingress"
  from_port                = 443
  to_port                  = 443
  protocol                 = "tcp"
  source_security_group_id = aws_security_group.node.id
  security_group_id        = aws_security_group.cluster.id
  description              = "Nodes to cluster API"
}

# ── Node Security Group ───────────────────────────────────────────────────────
resource "aws_security_group" "node" {
  name        = "${var.name_prefix}-eks-node-sg"
  description = "EKS managed node group — SpaceNet Fleet Management"
  vpc_id      = var.vpc_id

  ingress {
    from_port = 0
    to_port   = 0
    protocol  = "-1"
    self      = true
    description = "Node-to-node"
  }

  ingress {
    from_port                = 1025
    to_port                  = 65535
    protocol                 = "tcp"
    source_security_group_id = aws_security_group.cluster.id
    description              = "Control plane to node kubelets"
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound"
  }

  tags = merge(var.tags, { Name = "${var.name_prefix}-eks-node-sg" })
}

# ── OIDC Provider (for IRSA) ──────────────────────────────────────────────────
data "tls_certificate" "eks" {
  url = aws_eks_cluster.this.identity[0].oidc[0].issuer
}

resource "aws_iam_openid_connect_provider" "eks" {
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = [data.tls_certificate.eks.certificates[0].sha1_fingerprint]
  url             = aws_eks_cluster.this.identity[0].oidc[0].issuer
  tags            = var.tags
}

# ── IAM — Node Role ───────────────────────────────────────────────────────────
resource "aws_iam_role" "node" {
  name = "${var.name_prefix}-eks-node-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "node_policies" {
  for_each = toset([
    "arn:aws:iam::aws:policy/AmazonEKSWorkerNodePolicy",
    "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy",
    "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
    "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
  ])
  role       = aws_iam_role.node.name
  policy_arn = each.value
}

# ── Managed Node Groups ───────────────────────────────────────────────────────
locals {
  node_groups = {
    system = {
      instance_types = ["m6i.large"]
      min_size       = 2
      max_size       = 4
      desired_size   = 2
      capacity_type  = "ON_DEMAND"
      labels         = { role = "system" }
      taints         = [{ key = "CriticalAddonsOnly", value = "true", effect = "NO_SCHEDULE" }]
    }
    fleet_ops = {
      instance_types = ["m6i.xlarge"]
      min_size       = 3
      max_size       = 10
      desired_size   = 3
      capacity_type  = "ON_DEMAND"
      labels         = { role = "fleet-ops" }
      taints         = []
    }
    spot = {
      instance_types = ["m6i.xlarge", "m5.xlarge", "m5a.xlarge"]
      min_size       = 0
      max_size       = 20
      desired_size   = 0
      capacity_type  = "SPOT"
      labels         = { role = "spot-workers" }
      taints         = [{ key = "spot", value = "true", effect = "NO_SCHEDULE" }]
    }
  }
}

resource "aws_eks_node_group" "groups" {
  for_each = local.node_groups

  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "${var.name_prefix}-${each.key}"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = var.private_subnet_ids
  instance_types  = each.value.instance_types
  capacity_type   = each.value.capacity_type

  scaling_config {
    min_size     = each.value.min_size
    max_size     = each.value.max_size
    desired_size = each.value.desired_size
  }

  update_config { max_unavailable = 1 }

  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version_number
  }

  dynamic "taint" {
    for_each = each.value.taints
    content {
      key    = taint.value.key
      value  = taint.value.value
      effect = taint.value.effect
    }
  }

  labels = each.value.labels

  depends_on = [aws_iam_role_policy_attachment.node_policies]
  tags       = merge(var.tags, { Name = "${var.name_prefix}-ng-${each.key}" })
}

resource "aws_launch_template" "node" {
  name_prefix = "${var.name_prefix}-eks-node-"
  description = "SpaceNet EKS node launch template"

  vpc_security_group_ids = [aws_security_group.node.id]

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = 50
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = var.kms_key_arn
      delete_on_termination = true
    }
  }

  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"  # IMDSv2 mandatory
    http_put_response_hop_limit = 1
  }

  monitoring { enabled = true }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(var.tags, { Name = "${var.name_prefix}-eks-node" })
  }

  tags = var.tags
}

# ── EKS Add-ons ───────────────────────────────────────────────────────────────
locals {
  addons = {
    vpc-cni            = { version = "v1.18.1-eksbuild.1" }
    coredns            = { version = "v1.11.1-eksbuild.9" }
    kube-proxy         = { version = "v1.30.0-eksbuild.3" }
    aws-ebs-csi-driver = { version = "v1.29.1-eksbuild.1" }
    eks-pod-identity-agent = { version = "v1.3.0-eksbuild.1" }
  }
}

resource "aws_eks_addon" "addons" {
  for_each = local.addons

  cluster_name             = aws_eks_cluster.this.name
  addon_name               = each.key
  addon_version            = each.value.version
  resolve_conflicts_on_create = "OVERWRITE"
  resolve_conflicts_on_update = "OVERWRITE"

  tags = merge(var.tags, { Addon = each.key })
}

# ── IRSA — Fleet Management Microservice Roles ───────────────────────────────
locals {
  oidc_issuer_url = aws_eks_cluster.this.identity[0].oidc[0].issuer
  oidc_arn        = aws_iam_openid_connect_provider.eks.arn
  oidc_id         = replace(local.oidc_issuer_url, "https://", "")

  service_accounts = {
    contact_scheduler   = { namespace = "fleet-ops", sa = "contact-scheduler" }
    telemetry_ingestor  = { namespace = "fleet-ops", sa = "telemetry-ingestor" }
    command_dispatcher  = { namespace = "fleet-ops", sa = "command-dispatcher" }
    orbit_propagator    = { namespace = "fleet-ops", sa = "orbit-propagator" }
    anomaly_detector    = { namespace = "fleet-ops", sa = "anomaly-detector" }
    fleet_state_manager = { namespace = "fleet-ops", sa = "fleet-state-manager" }
    contact_reporter    = { namespace = "fleet-ops", sa = "contact-reporter" }
  }
}

resource "aws_iam_role" "service_accounts" {
  for_each = local.service_accounts

  name = "${var.name_prefix}-eks-sa-${replace(each.key, "_", "-")}"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = local.oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_id}:aud" = "sts.amazonaws.com"
          "${local.oidc_id}:sub" = "system:serviceaccount:${each.value.namespace}:${each.value.sa}"
        }
      }
    }]
  })

  tags = merge(var.tags, { ServiceAccount = each.value.sa })
}

# Telemetry Ingestor — Kinesis + Timestream + DynamoDB
resource "aws_iam_role_policy" "telemetry_ingestor" {
  name   = "telemetry-ingestor-policy"
  role   = aws_iam_role.service_accounts["telemetry_ingestor"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kinesis:GetRecords", "kinesis:GetShardIterator",
                    "kinesis:DescribeStream", "kinesis:ListShards"]
        Resource = var.kinesis_stream_arns
      },
      {
        Effect   = "Allow"
        Action   = ["timestream:WriteRecords", "timestream:DescribeEndpoints"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = var.dynamodb_table_arns
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = [var.kms_key_arn]
      }
    ]
  })
}

# Command Dispatcher — Ground Station + Step Functions + KMS signing
resource "aws_iam_role_policy" "command_dispatcher" {
  name   = "command-dispatcher-policy"
  role   = aws_iam_role.service_accounts["command_dispatcher"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["groundstation:CreateContact", "groundstation:DescribeContact",
                    "groundstation:ListContacts", "groundstation:GetConfig"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["states:StartExecution", "states:DescribeExecution"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        Resource = var.dynamodb_table_arns
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Sign", "kms:Verify", "kms:DescribeKey"]
        Resource = [var.command_signing_key_arn]
      }
    ]
  })
}

# Contact Scheduler — Ground Station + DynamoDB + EventBridge
resource "aws_iam_role_policy" "contact_scheduler" {
  name   = "contact-scheduler-policy"
  role   = aws_iam_role.service_accounts["contact_scheduler"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["groundstation:ReserveContact", "groundstation:CancelContact",
                    "groundstation:ListContacts", "groundstation:ListGroundStations",
                    "groundstation:GetMinuteUsage", "groundstation:ListMissionProfiles"]
        Resource = ["*"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query",
                    "dynamodb:GetItem", "dynamodb:Scan"]
        Resource = var.dynamodb_table_arns
      },
      {
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = ["*"]
      }
    ]
  })
}

# Anomaly Detector — Kinesis + SageMaker + SNS
resource "aws_iam_role_policy" "anomaly_detector" {
  name   = "anomaly-detector-policy"
  role   = aws_iam_role.service_accounts["anomaly_detector"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["kinesis:GetRecords", "kinesis:GetShardIterator",
                    "kinesis:DescribeStream", "kinesis:ListShards"]
        Resource = var.kinesis_stream_arns
      },
      {
        Effect   = "Allow"
        Action   = ["sagemaker:InvokeEndpoint"]
        Resource = ["arn:aws:sagemaker:*:*:endpoint/${var.name_prefix}-anomaly*"]
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.sns_alert_arns
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
        Resource = var.dynamodb_table_arns
      }
    ]
  })
}

# ── EKS Namespaces (via Kubernetes provider) ──────────────────────────────────
resource "kubernetes_namespace" "fleet_ops" {
  metadata {
    name = "fleet-ops"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
      "spacenet.io/environment"      = "prod"
      "istio-injection"              = "enabled"
    }
  }
}

resource "kubernetes_namespace" "monitoring" {
  metadata {
    name = "monitoring"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

resource "kubernetes_namespace" "flux_system" {
  metadata {
    name = "flux-system"
    labels = {
      "app.kubernetes.io/managed-by" = "terraform"
    }
  }
}

# ── Helm — AWS Load Balancer Controller ───────────────────────────────────────
resource "helm_release" "aws_load_balancer_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  namespace  = "kube-system"
  version    = "1.8.1"

  set { name = "clusterName"; value = aws_eks_cluster.this.name }
  set { name = "serviceAccount.annotations.eks\\.amazonaws\\.com/role-arn"
        value = aws_iam_role.aws_lb_controller.arn }
  set { name = "replicaCount"; value = "2" }
  set { name = "podDisruptionBudget.maxUnavailable"; value = "1" }

  depends_on = [aws_eks_node_group.groups]
}

resource "aws_iam_role" "aws_lb_controller" {
  name = "${var.name_prefix}-eks-aws-lb-controller"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = local.oidc_arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${local.oidc_id}:aud" = "sts.amazonaws.com"
          "${local.oidc_id}:sub" = "system:serviceaccount:kube-system:aws-load-balancer-controller"
        }
      }
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy_attachment" "aws_lb_controller" {
  role       = aws_iam_role.aws_lb_controller.name
  policy_arn = "arn:aws:iam::aws:policy/ElasticLoadBalancingFullAccess"
}

# ── Helm — External Secrets Operator ─────────────────────────────────────────
resource "helm_release" "external_secrets" {
  name       = "external-secrets"
  repository = "https://charts.external-secrets.io"
  chart      = "external-secrets"
  namespace  = "kube-system"
  version    = "0.9.17"

  set { name = "installCRDs"; value = "true" }
  set { name = "replicaCount"; value = "2" }

  depends_on = [aws_eks_node_group.groups]
}

# ── Helm — ADOT Collector (AWS Distro for OpenTelemetry) ─────────────────────
resource "helm_release" "adot" {
  name       = "adot-collector"
  repository = "https://aws-observability.github.io/aws-otel-helm-charts"
  chart      = "adot-exporter-for-eks-on-ec2"
  namespace  = "monitoring"
  version    = "0.6.0"

  set { name = "clusterName"; value = aws_eks_cluster.this.name }
  set { name = "awsRegion";   value = data.aws_region.current.name }

  depends_on = [kubernetes_namespace.monitoring, aws_eks_node_group.groups]
}

# ── Step Functions — TC Command Approval State Machine ────────────────────────
resource "aws_iam_role" "sfn" {
  name = "${var.name_prefix}-tc-approval-sfn-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "states.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = var.tags
}

resource "aws_iam_role_policy" "sfn" {
  name = "tc-approval-sfn-policy"
  role = aws_iam_role.sfn.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = ["arn:aws:lambda:*:${data.aws_caller_identity.current.account_id}:function:${var.name_prefix}-*"]
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem"]
        Resource = var.dynamodb_table_arns
      },
      {
        Effect   = "Allow"
        Action   = ["sns:Publish"]
        Resource = var.sns_alert_arns
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogDelivery", "logs:GetLogDelivery",
                    "logs:UpdateLogDelivery", "logs:DeleteLogDelivery",
                    "logs:ListLogDeliveries", "logs:PutResourcePolicy",
                    "logs:DescribeResourcePolicies", "logs:DescribeLogGroups"]
        Resource = ["*"]
      }
    ]
  })
}

resource "aws_sfn_state_machine" "tc_approval" {
  name     = "${var.name_prefix}-tc-command-approval"
  role_arn = aws_iam_role.sfn.arn

  definition = jsonencode({
    Comment = "SpaceNet TC Command Dual-Operator Approval Workflow"
    StartAt = "ValidateCommand"
    States = {
      ValidateCommand = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = "${var.name_prefix}-tc-validator"
          "Payload.$"  = "$$"
        }
        Next           = "RequestPrimaryApproval"
        ResultPath     = "$.validation"
        TimeoutSeconds = 30
      }
      RequestPrimaryApproval = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke.waitForTaskToken"
        Parameters = {
          FunctionName = "${var.name_prefix}-approval-notifier"
          Payload = {
            "taskToken.$" = "$$.Task.Token"
            "command.$"   = "$.command"
            approver      = "primary"
          }
        }
        Next           = "CheckPrimaryApproval"
        TimeoutSeconds = 3600
        Catch = [{
          ErrorEquals = ["States.TaskFailed", "States.Timeout"]
          Next        = "CommandRejected"
        }]
      }
      CheckPrimaryApproval = {
        Type = "Choice"
        Choices = [{
          Variable     = "$.primaryApproval.approved"
          BooleanEquals = true
          Next          = "RequestSecondaryApproval"
        }]
        Default = "CommandRejected"
      }
      RequestSecondaryApproval = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke.waitForTaskToken"
        Parameters = {
          FunctionName = "${var.name_prefix}-approval-notifier"
          Payload = {
            "taskToken.$" = "$$.Task.Token"
            "command.$"   = "$.command"
            approver      = "secondary"
          }
        }
        Next           = "CheckSecondaryApproval"
        TimeoutSeconds = 3600
        Catch = [{
          ErrorEquals = ["States.TaskFailed", "States.Timeout"]
          Next        = "CommandRejected"
        }]
      }
      CheckSecondaryApproval = {
        Type = "Choice"
        Choices = [{
          Variable      = "$.secondaryApproval.approved"
          BooleanEquals = true
          Next          = "DispatchCommand"
        }]
        Default = "CommandRejected"
      }
      DispatchCommand = {
        Type     = "Task"
        Resource = "arn:aws:states:::lambda:invoke"
        Parameters = {
          FunctionName = "${var.name_prefix}-tc-dispatcher"
          "Payload.$"  = "$$"
        }
        Next           = "CommandDispatched"
        TimeoutSeconds = 60
      }
      CommandDispatched = {
        Type = "Succeed"
      }
      CommandRejected = {
        Type  = "Fail"
        Cause = "Command rejected or approval timeout"
        Error = "ApprovalRejected"
      }
    }
  })

  tags = merge(var.tags, { Name = "${var.name_prefix}-tc-approval" })
}
