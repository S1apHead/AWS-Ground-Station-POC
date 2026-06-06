output "dataflow_endpoint_group_id" { value = aws_groundstation_dataflow_endpoint_group.this.id }
output "mission_profile_arn"        { value = aws_groundstation_mission_profile.leo_ttc.arn }
output "ground_station_role_arn"    { value = aws_iam_role.ground_station.arn }
output "dataflow_sg_id"             { value = aws_security_group.dataflow_endpoint.id }
