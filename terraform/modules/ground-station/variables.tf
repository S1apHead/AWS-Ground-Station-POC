variable "name_prefix"               { type = string }
variable "aws_region"                { type = string }
variable "vpc_id"                    { type = string }
variable "vpc_cidr"                  { type = string }
variable "subnet_ids"                { type = list(string) }
variable "dataflow_endpoint_ip"      { type = string; default = "10.10.2.10" }
variable "ground_station_cidr_blocks"{ type = list(string) }
variable "kinesis_stream_arns"       { type = list(string); default = [] }
variable "sns_alert_arns"            { type = list(string); default = [] }
variable "tags"                      { type = map(string); default = {} }
