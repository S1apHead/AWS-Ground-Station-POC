# =============================================================================
# Environment: prod/fleet-management
# Wires: networking + eks-fleet modules
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws        = { source = "hashicorp/aws",        version = "~> 5.0" }
    kubernetes = { source = "hashicorp/kubernetes",  version = "~> 2.27" }
    helm       = { source = "hashicorp/helm",        version = "~> 2.13" }
    tls        = { source = "hashicorp/tls",         version = "~> 4.0" }
  }
  backend "s3" {
    bucket         = "spacenet-terraform-state"
    key            = "prod/fleet-management/terraform.tfstate"
    region         = "ap-southeast-2"
    encrypt        = true
    kms_key_id     = "alias/spacenet-s3_frames"
    dynamodb_table = "spacenet-terraform-locks"
  }
}

provider "aws" {
  region = var.aws_region
  default_tags { tags = local.common_tags }
}

provider "kubernetes" {
  host                   = module.eks_fleet.cluster_endpoint
  cluster_ca_certificate = base64decode(module.eks_fleet.cluster_ca_certificate)
  exec {
    api_version = "client.authentication.k8s.io/v1beta1"
    command     = "aws"
    args        = ["eks", "get-token", "--cluster-name", module.eks_fleet.cluster_name]
  }
}

provider "helm" {
  kubernetes {
    host                   = module.eks_fleet.cluster_endpoint
    cluster_ca_certificate = base64decode(module.eks_fleet.cluster_ca_certificate)
    exec {
      api_version = "client.authentication.k8s.io/v1beta1"
      command     = "aws"
      args        = ["eks", "get-token", "--cluster-name", module.eks_fleet.cluster_name]
    }
  }
}

locals {
  common_tags = {
    Environment = "prod"
    Project     = "spacenet-ground-station"
    ManagedBy   = "terraform"
    Owner       = "platform-engineering"
    CostCentre  = "fleet-management"
  }
}

data "terraform_remote_state" "security" {
  backend = "s3"
  config = {
    bucket = "spacenet-terraform-state"
    key    = "prod/security/terraform.tfstate"
    region = "ap-southeast-2"
  }
}

data "terraform_remote_state" "data_pipeline" {
  backend = "s3"
  config = {
    bucket = "spacenet-terraform-state"
    key    = "prod/data-pipeline/terraform.tfstate"
    region = "ap-southeast-2"
  }
}

module "networking" {
  source = "../../../modules/networking"

  name_prefix            = "spacenet-fm"
  aws_region             = var.aws_region
  vpc_cidr               = "10.20.0.0/20"
  private_subnet_cidrs   = ["10.20.0.0/24", "10.20.1.0/24", "10.20.2.0/24"]
  protected_subnet_cidrs = []
  availability_zones     = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]
  kms_key_arn            = data.terraform_remote_state.security.outputs.primary_kms_key_arn
  enable_nat_gateway     = true
  enable_interface_endpoints = true
  tags                   = local.common_tags
}

module "eks_fleet" {
  source = "../../../modules/eks-fleet"

  name_prefix             = "spacenet"
  vpc_id                  = module.networking.vpc_id
  private_subnet_ids      = module.networking.private_subnet_ids
  kms_key_arn             = data.terraform_remote_state.security.outputs.primary_kms_key_arn
  command_signing_key_arn = data.terraform_remote_state.security.outputs.primary_kms_key_arn
  kinesis_stream_arns     = values(data.terraform_remote_state.data_pipeline.outputs.stream_arns)
  dynamodb_table_arns     = ["arn:aws:dynamodb:${var.aws_region}:*:table/spacenet-*"]
  sns_alert_arns          = [data.terraform_remote_state.security.outputs.sns_noc_alert_arn]
  kubernetes_version      = "1.30"
  tags                    = local.common_tags
}

output "cluster_name"         { value = module.eks_fleet.cluster_name }
output "cluster_endpoint"     { value = module.eks_fleet.cluster_endpoint }
output "tc_approval_sfn_arn"  { value = module.eks_fleet.tc_approval_sfn_arn }
