variable "name_prefix"            { type = string }
variable "vpc_id"                  { type = string }
variable "private_subnet_ids"      { type = list(string) }
variable "kms_key_arn"             { type = string }
variable "command_signing_key_arn" { type = string }
variable "kinesis_stream_arns"     { type = list(string) }
variable "dynamodb_table_arns"     { type = list(string) }
variable "sns_alert_arns"          { type = list(string); default = [] }
variable "kubernetes_version"      { type = string; default = "1.30" }
variable "tags"                    { type = map(string); default = {} }
