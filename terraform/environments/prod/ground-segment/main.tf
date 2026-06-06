# =============================================================================
# Environment: prod/ground-segment
# Wires: networking + ground-station + ecs-dataflow modules
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  backend "s3" {
    bucket         = "spacenet-terraform-state"
    key            = "prod/ground-segment/terraform.tfstate"
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

locals {
  common_tags = {
    Environment = "prod"
    Project     = "spacenet-ground-station"
    ManagedBy   = "terraform"
    Owner       = "platform-engineering"
    CostCentre  = "ground-segment"
  }
}

# ── Remote State (security account outputs) ───────────────────────────────────
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

# ── Networking ────────────────────────────────────────────────────────────────
module "networking" {
  source = "../../../modules/networking"

  name_prefix            = "spacenet-gs"
  aws_region             = var.aws_region
  vpc_cidr               = "10.10.0.0/20"
  private_subnet_cidrs   = ["10.10.0.0/24", "10.10.1.0/24", "10.10.2.0/24"]
  protected_subnet_cidrs = []
  availability_zones     = ["${var.aws_region}a", "${var.aws_region}b", "${var.aws_region}c"]
  kms_key_arn            = data.terraform_remote_state.security.outputs.primary_kms_key_arn
  enable_nat_gateway     = false
  enable_interface_endpoints = true
  tags                   = local.common_tags
}

# ── Ground Station ────────────────────────────────────────────────────────────
module "ground_station" {
  source = "../../../modules/ground-station"

  name_prefix                = "spacenet-gs"
  aws_region                 = var.aws_region
  vpc_id                     = module.networking.vpc_id
  vpc_cidr                   = module.networking.vpc_cidr
  subnet_ids                 = module.networking.private_subnet_ids
  ground_station_cidr_blocks = var.ground_station_cidr_blocks
  kinesis_stream_arns        = [data.terraform_remote_state.data_pipeline.outputs.hk_stream_arn]
  sns_alert_arns             = [data.terraform_remote_state.security.outputs.sns_noc_alert_arn]
  tags                       = local.common_tags
}

# ── ECS Dataflow Endpoint ─────────────────────────────────────────────────────
module "ecs_dataflow" {
  source = "../../../modules/ecs-dataflow"

  name_prefix                = "spacenet-gs"
  aws_region                 = var.aws_region
  vpc_id                     = module.networking.vpc_id
  vpc_cidr                   = module.networking.vpc_cidr
  subnet_ids                 = module.networking.private_subnet_ids
  kms_key_arn                = data.terraform_remote_state.security.outputs.primary_kms_key_arn
  kinesis_stream_arns        = values(data.terraform_remote_state.data_pipeline.outputs.stream_arns)
  kinesis_hk_stream_name     = data.terraform_remote_state.data_pipeline.outputs.hk_stream_name
  kinesis_raw_stream_name    = data.terraform_remote_state.data_pipeline.outputs.raw_stream_name
  s3_raw_frames_arn          = data.terraform_remote_state.data_pipeline.outputs.raw_frames_bucket_arn
  s3_raw_frames_bucket       = data.terraform_remote_state.data_pipeline.outputs.raw_frames_bucket_name
  ground_station_cidr_blocks = var.ground_station_cidr_blocks
  iot_endpoint_secret_arn    = data.terraform_remote_state.security.outputs.iot_endpoint_secret_arn
  sns_alert_arns             = [data.terraform_remote_state.security.outputs.sns_noc_alert_arn]
  desired_count              = 1
  tags                       = local.common_tags
}
