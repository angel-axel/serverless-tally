output "caller_identity" {
  description = "AWS account, user ID and ARN of the identity Terraform is running as."
  value       = data.aws_caller_identity.caller_identity
}

output "region" {
  description = "AWS region where the resources are deployed."
  value       = data.aws_region.region
}

output "gatekeeper_function_name" {
  description = "Name of the gatekeeper Lambda function, useful to find its logs in CloudWatch."
  value       = aws_lambda_function.lambda_function_gatekeeper.function_name
}

output "dispatcher_function_name" {
  description = "Name of the dispatcher Lambda function, useful to find its logs in CloudWatch."
  value       = aws_lambda_function.lambda_function_dispatcher.function_name
}

output "tool_function_names" {
  description = "Names of the Lambda functions behind the agent's tools, useful to find their logs in CloudWatch."
  value = {
    read_invoice     = aws_lambda_function.lambda_function_read_invoice.function_name
    find_open_orders = aws_lambda_function.lambda_function_find_open_orders.function_name
    propose_match    = aws_lambda_function.lambda_function_propose_match.function_name
  }
}

output "decision_function_name" {
  description = "Name of the decision Lambda function, useful to find its logs in CloudWatch."
  value       = aws_lambda_function.lambda_function_decision.function_name
}