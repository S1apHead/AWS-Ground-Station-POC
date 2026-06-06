variable "name_prefix"                { type = string }
variable "aws_region"                 { type = string }
variable "vpc_id"                     { type = string }
variable "vpc_cidr"                   { type = string }
variable "subnet_ids"                 { type = list(string) }
variable "kms_key_arn"                { type = string }
variable "kinesis_stream_arns"        { type = list(string) }
variable "kinesis_hk_stream_name"     { type = string }
variable "kinesis_raw_stream_name"    { type = string }
variable "s3_raw_frames_arn"          { type = string }
variable "s3_raw_frames_bucket"       { type = string }
variable "ground_station_cidr_blocks" { type = list(string) }
variable "iot_endpoint_secret_arn"    { type = string }
variable "secret_arns"                { type = list(string); default = [] }
variable "sns_alert_arns"             { type = list(string); default = [] }
variable "desired_count"              { type = number; default = 1 }
variable "tags"                       { type = map(string); default = {} }
