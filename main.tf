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
  bucket = "${var.spa_bucket_name}-${data.aws_caller_identity.caller_identity.account_id}-${data.aws_region.region.name}-an"
  domain = "${var.subdomain}.${var.domain}"
}

resource "aws_s3_bucket" "s3_bucket" {
  bucket           = local.bucket
  bucket_namespace = "account-regional"
}

resource "aws_s3_bucket_policy" "s3_bucket_policy" {
  bucket = aws_s3_bucket.s3_bucket.id
  policy = data.aws_iam_policy_document.origin_bucket_policy.json
}

resource "aws_acm_certificate" "acm_certificate" {
  domain_name               = local.domain
  validation_method         = "DNS"
  subject_alternative_names = ["*.${local.domain}"]
}

resource "aws_acm_certificate_validation" "acm_certificate_validation" {
  certificate_arn         = aws_acm_certificate.acm_certificate.arn
  validation_record_fqdns = [for record in aws_route53_record.acm_certificate_validation : record.fqdn]

  timeouts {
    create = "15m"
  }
}

resource "aws_route53_record" "acm_certificate_validation" {
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
  ttl             = var.dns_records_ttl
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

resource "aws_cloudfront_origin_access_control" "default" {
  name                              = "origin-access-control"
  origin_access_control_origin_type = "s3"
  signing_behavior                  = "always"
  signing_protocol                  = "sigv4"
}

resource "aws_cloudfront_distribution" "cloudfront_distribution" {
  origin {
    domain_name              = aws_s3_bucket.s3_bucket.bucket_domain_name
    origin_access_control_id = aws_cloudfront_origin_access_control.default.id
    origin_id                = local.bucket
  }

  enabled             = true
  default_root_object = "index.html"

  aliases = [local.domain]

  default_cache_behavior {
    allowed_methods  = ["GET", "HEAD"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = local.bucket

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