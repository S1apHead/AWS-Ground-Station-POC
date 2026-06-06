# =============================================================================
# Environment: prod/data-pipeline
# Wires: kinesis-pipeline + data-storage modules
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  backend "s3" {
    bucket         = "spacenet-terraform-state"
    key            = "prod/data-pipeline/terraform.tfstate"
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
    CostCentre  = "data-pipeline"
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

module "data_storage" {
  source = "../../../modules/data-storage"

  name_prefix = "spacenet"
  aws_region  = var.aws_region
  dr_region   = var.dr_region
  kms_key_arns = {
    s3        = data.terraform_remote_state.security.outputs.primary_kms_key_arn
    dynamodb  = data.terraform_remote_state.security.outputs.primary_kms_key_arn
    timestream = data.terraform_remote_state.security.outputs.primary_kms_key_arn
    sqs       = data.terraform_remote_state.security.outputs.primary_kms_key_arn
  }
  tags = local.common_tags
}

module "kinesis_pipeline" {
  source = "../../../modules/kinesis-pipeline"

  name_prefix           = "spacenet"
  aws_region            = var.aws_region
  kms_key_arn           = data.terraform_remote_state.security.outputs.primary_kms_key_arn
  raw_frames_bucket_arn = module.data_storage.raw_frames_bucket_arn
  raw_frames_bucket     = module.data_storage.raw_frames_bucket_name
  timestream_database   = module.data_storage.timestream_database_name
  timestream_hk_table   = module.data_storage.timestream_hk_table
  dynamodb_anomalies    = module.data_storage.dynamodb_table_names["anomalies"]
  sns_alert_arns        = [data.terraform_remote_state.security.outputs.sns_noc_alert_arn]
  tags                  = local.common_tags
}

# ── Outputs consumed by other environment stacks ──────────────────────────────
output "hk_stream_arn"          { value = module.kinesis_pipeline.hk_stream_arn }
output "hk_stream_name"         { value = module.kinesis_pipeline.hk_stream_name }
output "raw_stream_arn"         { value = module.kinesis_pipeline.raw_stream_arn }
output "raw_stream_name"        { value = module.kinesis_pipeline.raw_stream_name }
output "stream_arns"            { value = module.kinesis_pipeline.stream_arns }
output "raw_frames_bucket_arn"  { value = module.data_storage.raw_frames_bucket_arn }
output "raw_frames_bucket_name" { value = module.data_storage.raw_frames_bucket_name }
