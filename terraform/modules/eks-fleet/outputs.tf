output "cluster_name"            { value = aws_eks_cluster.this.name }
output "cluster_endpoint"        { value = aws_eks_cluster.this.endpoint }
output "cluster_ca_certificate"  { value = aws_eks_cluster.this.certificate_authority[0].data }
output "oidc_provider_arn"       { value = aws_iam_openid_connect_provider.eks.arn }
output "node_role_arn"           { value = aws_iam_role.node.arn }
output "cluster_security_group_id" { value = aws_security_group.cluster.id }
output "tc_approval_sfn_arn"     { value = aws_sfn_state_machine.tc_approval.arn }
output "service_account_role_arns" { value = { for k, v in aws_iam_role.service_accounts : k => v.arn } }
