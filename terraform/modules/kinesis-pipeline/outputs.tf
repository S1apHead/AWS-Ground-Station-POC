output "stream_arns"          { value = { for k, v in aws_kinesis_stream.streams : k => v.arn } }
output "stream_names"         { value = { for k, v in aws_kinesis_stream.streams : k => v.name } }
output "hk_stream_arn"        { value = aws_kinesis_stream.streams["hk"].arn }
output "hk_stream_name"       { value = aws_kinesis_stream.streams["hk"].name }
output "raw_stream_name"      { value = aws_kinesis_stream.streams["raw"].name }
output "lambda_hk_arn"        { value = aws_lambda_function.hk_consumer.arn }
output "firehose_stream_name" { value = aws_kinesis_firehose_delivery_stream.raw_frames.name }
