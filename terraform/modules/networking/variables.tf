variable "name_prefix"              { type = string }
variable "aws_region"               { type = string }
variable "vpc_cidr"                 { type = string }
variable "private_subnet_cidrs"     { type = list(string) }
variable "protected_subnet_cidrs"   { type = list(string); default = [] }
variable "public_subnet_cidrs"      { type = list(string); default = [] }
variable "availability_zones"       { type = list(string) }
variable "kms_key_arn"              { type = string; default = "" }
variable "enable_nat_gateway"       { type = bool; default = false }
variable "enable_interface_endpoints" { type = bool; default = true }
variable "enable_bastion"           { type = bool; default = false }
variable "tags"                     { type = map(string); default = {} }
