variable "name_prefix"              { type = string }
variable "aws_region"               { type = string }
variable "vpc_id"                   { type = string }
variable "kms_key_id"               { type = string }
variable "kms_key_arn"              { type = string }
variable "s3_raw_frames_arn"        { type = string }
variable "timestream_database_name" { type = string }
variable "timestream_hk_table_name" { type = string }
variable "ecr_base_url"             { type = string }
variable "lambda_subnet_ids"        { type = list(string) }
variable "sqs_dlq_arn"              { type = string }
variable "sns_alert_arns"           { type = list(string); default = [] }
variable "tags"                     { type = map(string); default = {} }
