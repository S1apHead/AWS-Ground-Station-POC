output "bucket_arns"              { value = { for k, v in aws_s3_bucket.buckets : k => v.arn } }
output "bucket_names"             { value = { for k, v in aws_s3_bucket.buckets : k => v.id } }
output "raw_frames_bucket_arn"    { value = aws_s3_bucket.buckets["raw_frames"].arn }
output "raw_frames_bucket_name"   { value = aws_s3_bucket.buckets["raw_frames"].id }
output "timestream_database_name" { value = aws_timestreamwrite_database.telemetry.database_name }
output "timestream_hk_table"      { value = aws_timestreamwrite_table.satellite_hk.table_name }
output "dynamodb_table_names"     { value = {
  satellites      = aws_dynamodb_table.satellites.name
  contacts        = aws_dynamodb_table.contacts.name
  telemetry_state = aws_dynamodb_table.telemetry_state.name
  commands        = aws_dynamodb_table.commands.name
  anomalies       = aws_dynamodb_table.anomalies.name
}}
output "sqs_dlq_arn"              { value = aws_sqs_queue.dlq.arn }
