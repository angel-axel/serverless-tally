data "aws_caller_identity" "caller_identity" {
}

data "aws_region" "region" {
}

data "aws_iam_policy_document" "origin_bucket_policy" {
  statement {
    sid    = "AllowCloudFrontServicePrincipalRead"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["cloudfront.amazonaws.com"]
    }

    actions = [
      "s3:GetObject",
    ]

    resources = ["${aws_s3_bucket.s3_bucket.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.cloudfront_distribution.arn]
    }
  }
}

data "aws_route53_zone" "route53_zone" {
  name = var.domain
}

