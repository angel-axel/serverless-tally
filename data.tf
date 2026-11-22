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

    resources = ["${aws_s3_bucket.spa_s3_bucket.arn}/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = [aws_cloudfront_distribution.cloudfront_distribution.arn]
    }
  }
}

data "aws_iam_policy_document" "invoices_inbound_bucket_policy" {
  statement {
    sid    = "AllowSESPutObject"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["ses.amazonaws.com"]
    }

    actions = [
      "s3:PutObject",
    ]

    resources = ["${aws_s3_bucket.s3_bucket.arn}/emails/*"]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceAccount"
      values   = [data.aws_caller_identity.caller_identity.account_id]
    }

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceArn"
      values   = ["arn:aws:ses:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:receipt-rule-set/${var.ses_rule_set_name}:receipt-rule/${var.ses_rule_name}"]
    }
  }
}

data "aws_iam_policy_document" "gatekeeper_assume_role_policy" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "gatekeeper_lambda_policy" {
  statement {
    sid       = "ReadInboundEmails"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.s3_bucket.arn}/emails/*"]
  }

  statement {
    sid     = "WriteSortedObjects"
    effect  = "Allow"
    actions = ["s3:PutObject"]
    resources = [
      "${aws_s3_bucket.s3_bucket.arn}/invoices/pending/*",
      "${aws_s3_bucket.s3_bucket.arn}/non-invoices/*",
      "${aws_s3_bucket.s3_bucket.arn}/quarantine/*",
    ]
  }

  statement {
    sid       = "RegisterInvoices"
    effect    = "Allow"
    actions   = ["dynamodb:PutItem"]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.arn]
  }

  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.cloudwatch_log_group_gatekeeper_lambda.arn}:*"]
  }
}

data "aws_route53_zone" "route53_zone" {
  name = var.domain
}

data "archive_file" "gatekeeper_lambda_code" {
  type        = "zip"
  source_dir  = "${path.module}/lambdas/gatekeeper"
  output_path = "${path.module}/dist/lambdas/gatekeeper.zip"

  excludes = [
    "__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "tests",
    "tests/**",
    "README.md",
  ]
}