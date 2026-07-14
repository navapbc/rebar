output "qualified_arn" {
  description = "Published Lambda version ARN — associate this on the protected distribution's cache behavior (viewer-request)."
  value       = aws_lambda_function.gate.qualified_arn
}

output "function_name" {
  description = "The Lambda@Edge function name."
  value       = aws_lambda_function.gate.function_name
}

output "version" {
  description = "The published version number."
  value       = aws_lambda_function.gate.version
}
