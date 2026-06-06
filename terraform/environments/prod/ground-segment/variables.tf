variable "aws_region" {
  type    = string
  default = "ap-southeast-2"
}

variable "ground_station_cidr_blocks" {
  type    = list(string)
  default = ["35.190.0.0/16", "34.64.0.0/10"]
  description = "CIDR blocks for AWS Ground Station service IPs"
}
