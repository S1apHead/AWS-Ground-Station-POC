# =============================================================================
# Environment: prod/security
# =============================================================================

terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.0" }
  }
  backend "s3" {
    bucket         = "spacenet-terraform-state"
    key            = "prod/security/terraform.tfstate"
    region         = "ap-southeast-2"
    encrypt        = true
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
    Owner       = "security"
    CostCentre  = "security"
  }
}

data "aws_caller_identity" "current" {}

module "security" {
  source = "../../../modules/security"

  name_prefix         = "spacenet"
  kms_admin_role_arns = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/PlatformAdmin"]
  kms_user_role_arns  = [
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/ECSTaskRole",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/EKSNodeRole",
    "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/LambdaExecutionRole",
  ]
  is_org_trail        = false
  tags                = local.common_tags
}

output "primary_kms_key_arn"   { value = module.security.primary_kms_key_arn }
output "sns_noc_alert_arn"     { value = module.security.sns_noc_alert_arn }
output "iot_endpoint_secret_arn" { value = module.security.iot_endpoint_secret_arn }
