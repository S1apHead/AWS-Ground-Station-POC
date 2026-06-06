output "cluster_arn"          { value = aws_ecs_cluster.dataflow.arn }
output "service_name"         { value = aws_ecs_service.dataflow_endpoint.name }
output "task_definition_arn"  { value = aws_ecs_task_definition.dataflow_endpoint.arn }
output "ecr_repository_url"   { value = aws_ecr_repository.dataflow_endpoint.repository_url }
output "task_role_arn"         { value = aws_iam_role.ecs_task.arn }
output "log_group_name"        { value = aws_cloudwatch_log_group.dataflow.name }
