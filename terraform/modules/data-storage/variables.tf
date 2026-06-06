variable "name_prefix"   { type = string }
variable "aws_region"    { type = string }
variable "dr_region"     { type = string; default = "us-east-1" }
variable "kms_key_arn"   { type = string }
variable "kms_key_id"    { type = string }
variable "environment"   { type = string; default = "prod" }
variable "tags"          { type = map(string); default = {} }
