output "caller_identity" {
  value = data.aws_caller_identity.caller_identity
}

output "region" {
  value = data.aws_region.region
}
