output "vpc_id"              { value = aws_vpc.this.id }
output "private_subnet_ids"  { value = aws_subnet.private[*].id }
output "protected_subnet_ids"{ value = aws_subnet.protected[*].id }
output "vpc_cidr"             { value = aws_vpc.this.cidr_block }
output "flow_log_group"       { value = aws_cloudwatch_log_group.flow_logs.name }
