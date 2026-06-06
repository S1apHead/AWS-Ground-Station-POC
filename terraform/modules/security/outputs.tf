output "kms_key_arns"             { value = { for k, v in aws_kms_key.keys : k => v.arn } }
output "kms_key_ids"              { value = { for k, v in aws_kms_key.keys : k => v.key_id } }
output "primary_kms_key_arn"      { value = aws_kms_key.keys["s3_frames"].arn }
output "primary_kms_key_id"       { value = aws_kms_key.keys["s3_frames"].key_id }
output "secrets_kms_key_arn"      { value = aws_kms_key.keys["secrets"].arn }
output "secrets_kms_key_id"       { value = aws_kms_key.keys["secrets"].key_id }
output "sns_security_alert_arn"   { value = aws_sns_topic.security_alerts.arn }
output "sns_noc_alert_arn"        { value = aws_sns_topic.noc_alerts.arn }
output "guardduty_detector_id"    { value = aws_guardduty_detector.this.id }
output "iot_endpoint_secret_arn"  { value = aws_secretsmanager_secret.iot_endpoint.arn }
output "cloudtrail_bucket_name"   { value = aws_s3_bucket.cloudtrail.id }
