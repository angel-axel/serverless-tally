terraform {
  required_version = ">= 1.17.0, < 1.18.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.73.0, < 6.74.0"
    }
  }

  backend "s3" {
    key          = "tally/terraform.tfstate"
    use_lockfile = true
  }
}

locals {
  s3_namespace_suffix = "-${data.aws_caller_identity.caller_identity.account_id}-${data.aws_region.region.region}-an"

  spa_bucket = "${var.spa_bucket_name}${local.s3_namespace_suffix}"

  bucket = "${var.bucket_name}${local.s3_namespace_suffix}"

  domain = "${var.subdomain}.${var.domain}"

  invoice_matcher_model_ids = distinct([
    for model in data.aws_bedrock_foundation_models.text_models.model_summaries :
    model.model_id if model.model_name == var.invoice_matcher_model_name && length(split(":", model.model_id)) <= 2
  ])
}

resource "aws_s3_bucket" "spa_s3_bucket" {
  bucket           = local.spa_bucket
  bucket_namespace = "account-regional"
}

resource "aws_s3_bucket_policy" "spa_s3_bucket_policy" {
  bucket = aws_s3_bucket.spa_s3_bucket.id
  policy = data.aws_iam_policy_document.origin_bucket_policy.json
}

resource "aws_acm_certificate" "acm_certificate" {
  domain_name               = local.domain
  validation_method         = "DNS"
  subject_alternative_names = ["*.${local.domain}"]
}

resource "aws_acm_certificate_validation" "acm_certificate_validation" {
  certificate_arn         = aws_acm_certificate.acm_certificate.arn
  validation_record_fqdns = [for record in aws_route53_record.acm_certificate_validation_records : record.fqdn]

  timeouts {
    create = "15m"
  }
}

resource "aws_route53_record" "acm_certificate_validation_records" {
  for_each = {
    for validation_option in aws_acm_certificate.acm_certificate.domain_validation_options : validation_option.domain_name => {
      name   = validation_option.resource_record_name
      type   = validation_option.resource_record_type
      record = validation_option.resource_record_value
    }
  }

  zone_id         = data.aws_route53_zone.route53_zone.zone_id
  name            = each.value.name
  type            = each.value.type
  records         = [each.value.record]
  ttl             = var.acm_dns_records_ttl
  allow_overwrite = true
}

resource "aws_route53_record" "cloudfront_cname" {
  for_each = toset(["A", "AAAA"])

  zone_id = data.aws_route53_zone.route53_zone.zone_id
  name    = local.domain
  type    = each.key

  alias {
    name                   = aws_cloudfront_distribution.cloudfront_distribution.domain_name
    zone_id                = aws_cloudfront_distribution.cloudfront_distribution.hosted_zone_id
    evaluate_target_health = false
  }
}

resource "aws_cloudfront_origin_access_control" "cloudfront_origin_access_control_default" {
  name                              = "origin-access-control"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "cloudfront_distribution" {
  origin {
    domain_name              = aws_s3_bucket.spa_s3_bucket.bucket_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.cloudfront_origin_access_control_default.id
    origin_id                = local.spa_bucket
  }

  enabled             = true
  default_root_object = "index.html"

  aliases = [local.domain]

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = local.spa_bucket

    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }

    viewer_protocol_policy = "redirect-to-https"
    min_ttl                = var.cloudfront_min_ttl
    default_ttl            = var.cloudfront_default_ttl
    max_ttl                = var.cloudfront_max_ttl
  }

  viewer_certificate {
    acm_certificate_arn      = aws_acm_certificate.acm_certificate.arn
    ssl_support_method       = "sni-only"
    minimum_protocol_version = "TLSv1.2_2021"
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
}

resource "aws_ses_domain_identity" "ses_domain_identity" {
  domain = local.domain
}

resource "aws_route53_record" "ses_domain_identity_verification_records" {
  zone_id = data.aws_route53_zone.route53_zone.zone_id
  name    = "_amazonses.${aws_ses_domain_identity.ses_domain_identity.domain}"
  type    = "TXT"
  ttl     = var.ses_dns_record_txt_ttl
  records = [aws_ses_domain_identity.ses_domain_identity.verification_token]
}

resource "aws_ses_domain_identity_verification" "ses_domain_identity_verification" {
  domain     = aws_ses_domain_identity.ses_domain_identity.id
  depends_on = [aws_route53_record.ses_domain_identity_verification_records]
}

resource "aws_ses_domain_dkim" "ses_domain_dkim" {
  domain = aws_ses_domain_identity.ses_domain_identity.domain
}

resource "aws_route53_record" "ses_domain_dkim_verification_records" {
  count   = 3
  zone_id = data.aws_route53_zone.route53_zone.zone_id
  name    = "${aws_ses_domain_dkim.ses_domain_dkim.dkim_tokens[count.index]}._domainkey.${aws_ses_domain_identity.ses_domain_identity.domain}"
  type    = "CNAME"
  ttl     = var.ses_dns_record_dkim_ttl
  records = ["${aws_ses_domain_dkim.ses_domain_dkim.dkim_tokens[count.index]}.dkim.amazonses.com"]
}

resource "aws_route53_record" "ses_inbound_mx_record" {
  zone_id = data.aws_route53_zone.route53_zone.zone_id
  name    = local.domain
  type    = "MX"
  ttl     = var.ses_dns_record_mx_ttl
  records = ["10 inbound-smtp.${data.aws_region.region.region}.amazonaws.com"]
}

resource "aws_s3_bucket" "s3_bucket" {
  bucket           = local.bucket
  bucket_namespace = "account-regional"
}

resource "aws_s3_bucket_public_access_block" "s3_bucket_public_access_block" {
  bucket                  = aws_s3_bucket.s3_bucket.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "s3_bucket_server_side_encryption_configuration" {
  bucket = aws_s3_bucket.s3_bucket.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_policy" "s3_bucket_policy" {
  bucket = aws_s3_bucket.s3_bucket.id
  policy = data.aws_iam_policy_document.invoices_inbound_bucket_policy.json
}

resource "aws_ses_receipt_rule_set" "ses_receipt_rule_set" {
  rule_set_name = var.ses_rule_set_name
}

resource "aws_ses_active_receipt_rule_set" "ses_active_receipt_rule_set" {
  rule_set_name = aws_ses_receipt_rule_set.ses_receipt_rule_set.rule_set_name
}

resource "aws_ses_receipt_rule" "ses_receipt_rule" {
  name          = var.ses_rule_name
  rule_set_name = aws_ses_receipt_rule_set.ses_receipt_rule_set.rule_set_name
  recipients    = ["${var.ses_recipient_local_part}@${local.domain}"]
  enabled       = true
  scan_enabled  = true

  s3_action {
    bucket_name       = aws_s3_bucket.s3_bucket.id
    object_key_prefix = "emails/"
    position          = 1
  }

  depends_on = [
    aws_s3_bucket_policy.s3_bucket_policy,
    aws_ses_domain_identity_verification.ses_domain_identity_verification,
  ]
}

resource "aws_dynamodb_table" "invoices_dynamodb_table" {
  name         = var.invoices_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "uuid"

  attribute {
    name = "uuid"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_dynamodb_table" "purchase_orders_dynamodb_table" {
  name         = var.purchase_orders_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "supplier_rfc"
  range_key    = "order_id"

  attribute {
    name = "supplier_rfc"
    type = "S"
  }

  attribute {
    name = "order_id"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  server_side_encryption {
    enabled = true
  }
}

resource "aws_cloudwatch_log_group" "cloudwatch_log_group_gatekeeper_lambda" {
  name              = "/aws/lambda/${var.gatekeeper_function_name}"
  retention_in_days = var.gatekeeper_log_retention_days
}

resource "aws_iam_role" "iam_role_gatekeeper_lambda" {
  name               = "${var.gatekeeper_function_name}-role"
  assume_role_policy = data.aws_iam_policy_document.gatekeeper_assume_role_policy.json
}

resource "aws_iam_role_policy" "iam_role_policy_gatekeeper_lambda" {
  name   = "${var.gatekeeper_function_name}-permissions"
  role   = aws_iam_role.iam_role_gatekeeper_lambda.id
  policy = data.aws_iam_policy_document.gatekeeper_lambda_policy.json
}

resource "aws_lambda_function" "lambda_function_gatekeeper" {
  function_name    = var.gatekeeper_function_name
  role             = aws_iam_role.iam_role_gatekeeper_lambda.arn
  runtime          = "python3.13"
  handler          = "handler.handler"
  filename         = data.archive_file.gatekeeper_lambda_code.output_path
  source_code_hash = data.archive_file.gatekeeper_lambda_code.output_base64sha256
  timeout          = 30
  memory_size      = 256

  environment {
    variables = {
      INVOICES_TABLE = aws_dynamodb_table.invoices_dynamodb_table.name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.cloudwatch_log_group_gatekeeper_lambda,
    aws_iam_role_policy.iam_role_policy_gatekeeper_lambda,
  ]
}

resource "aws_lambda_permission" "lambda_permission_gatekeeper_s3_invoke" {
  statement_id   = "AllowS3InvokeGatekeeper"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.lambda_function_gatekeeper.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.s3_bucket.arn
  source_account = data.aws_caller_identity.caller_identity.account_id
}

resource "aws_s3_bucket_notification" "s3_bucket_notification" {
  bucket = aws_s3_bucket.s3_bucket.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.lambda_function_gatekeeper.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "emails/"
  }

  depends_on = [aws_lambda_permission.lambda_permission_gatekeeper_s3_invoke]
}

resource "aws_iam_role" "iam_role_invoice_matcher_agent" {
  name               = "${var.agent_name}-role"
  assume_role_policy = data.aws_iam_policy_document.invoice_matcher_assume_role_policy.json
}

resource "aws_iam_role_policy" "iam_role_policy_invoice_matcher_agent" {
  name   = "${var.agent_name}-permissions"
  role   = aws_iam_role.iam_role_invoice_matcher_agent.id
  policy = data.aws_iam_policy_document.invoice_matcher_agent_policy.json
}

resource "aws_bedrockagentcore_harness" "invoice_matcher_harness" {
  harness_name       = var.invoice_matcher_harness_name
  execution_role_arn = aws_iam_role.iam_role_invoice_matcher_agent.arn

  system_prompt {
    text = file("${path.module}/agents/invoice_matcher/instructions.md")
  }

  model {
    bedrock_model_config {
      model_id    = data.aws_bedrock_inference_profile.bedrock_inference_profile.inference_profile_id
      temperature = 0
    }
  }

  allowed_tools = ["todo_write"]

  max_iterations  = var.agent_max_iterations
  timeout_seconds = var.agent_timeout_seconds

  depends_on = [aws_iam_role_policy.iam_role_policy_invoice_matcher_agent]
}