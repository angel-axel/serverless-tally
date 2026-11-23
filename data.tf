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

data "aws_iam_policy_document" "lambda_assume_role_policy" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "read_invoice_lambda_policy" {
  statement {
    sid       = "ReadInvoiceRecord"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.arn]
  }

  statement {
    sid       = "ReadInvoiceXml"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.s3_bucket.arn}/invoices/pending/*"]
  }

  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.cloudwatch_log_group_read_invoice_lambda.arn}:*"]
  }
}

data "aws_iam_policy_document" "find_open_orders_lambda_policy" {
  statement {
    sid       = "ReadInvoiceSupplier"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.arn]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:Attributes"
      values   = ["uuid", "issuer_rfc", "status"]
    }

    condition {
      test     = "StringEqualsIfExists"
      variable = "dynamodb:Select"
      values   = ["SPECIFIC_ATTRIBUTES"]
    }
  }

  statement {
    sid       = "QueryPurchaseOrders"
    effect    = "Allow"
    actions   = ["dynamodb:Query"]
    resources = [aws_dynamodb_table.purchase_orders_dynamodb_table.arn]
  }

  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.cloudwatch_log_group_find_open_orders_lambda.arn}:*"]
  }
}

data "aws_iam_policy_document" "propose_match_lambda_policy" {
  statement {
    sid       = "ReadInvoiceRecord"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.arn]
  }

  statement {
    sid       = "RecordMatchProposal"
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.arn]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:Attributes"
      values   = ["uuid", "status", "match_proposal", "proposed_at"]
    }

    condition {
      test     = "StringEqualsIfExists"
      variable = "dynamodb:ReturnValues"
      values   = ["NONE", "UPDATED_OLD", "UPDATED_NEW"]
    }
  }

  statement {
    sid       = "ReadInvoiceXml"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.s3_bucket.arn}/invoices/pending/*"]
  }

  statement {
    sid       = "ReadPurchaseOrders"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem", "dynamodb:Query"]
    resources = [aws_dynamodb_table.purchase_orders_dynamodb_table.arn]
  }

  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.cloudwatch_log_group_propose_match_lambda.arn}:*"]
  }
}

data "aws_iam_policy_document" "decision_lambda_policy" {
  statement {
    sid    = "ReadInvoicesStream"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
    ]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.stream_arn]
  }

  statement {
    sid       = "ListStreams"
    effect    = "Allow"
    actions   = ["dynamodb:ListStreams"]
    resources = ["*"]
  }

  statement {
    sid       = "ReadInvoiceRecord"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.arn]
  }

  statement {
    sid       = "RecordDecision"
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.arn]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:Attributes"
      values   = ["uuid", "status", "decision_reasons", "decided_at"]
    }

    condition {
      test     = "StringEqualsIfExists"
      variable = "dynamodb:ReturnValues"
      values   = ["NONE", "UPDATED_OLD", "UPDATED_NEW"]
    }
  }

  statement {
    sid       = "ReadInvoiceXml"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.s3_bucket.arn}/invoices/pending/*"]
  }

  statement {
    sid       = "ReadPurchaseOrders"
    effect    = "Allow"
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.purchase_orders_dynamodb_table.arn]
  }

  statement {
    sid       = "RecordInvoicedQuantities"
    effect    = "Allow"
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.purchase_orders_dynamodb_table.arn]

    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:Attributes"
      values   = ["supplier_rfc", "order_id", "invoiced_quantities", "status"]
    }

    condition {
      test     = "StringEqualsIfExists"
      variable = "dynamodb:ReturnValues"
      values   = ["NONE", "UPDATED_OLD", "UPDATED_NEW"]
    }
  }

  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.cloudwatch_log_group_decision_lambda.arn}:*"]
  }
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

data "aws_iam_policy_document" "dispatcher_assume_role_policy" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "dispatcher_lambda_policy" {
  statement {
    sid    = "ReadInvoicesStream"
    effect = "Allow"
    actions = [
      "dynamodb:DescribeStream",
      "dynamodb:GetRecords",
      "dynamodb:GetShardIterator",
    ]
    resources = [aws_dynamodb_table.invoices_dynamodb_table.stream_arn]
  }

  statement {
    sid       = "ListStreams"
    effect    = "Allow"
    actions   = ["dynamodb:ListStreams"]
    resources = ["*"]
  }

  statement {
    sid    = "InvokeInvoiceMatcherAgent"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:InvokeHarness",
      "bedrock-agentcore:InvokeAgentRuntime",
    ]
    resources = [aws_bedrockagentcore_harness.invoice_matcher_harness.arn]
  }

  statement {
    sid       = "WriteLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.cloudwatch_log_group_dispatcher_lambda.arn}:*"]
  }

  statement {
    sid    = "WriteTraces"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
    ]
    resources = ["*"]
  }
}

data "archive_file" "dispatcher_lambda_code" {
  type        = "zip"
  source_dir  = "${path.module}/lambdas/dispatcher"
  output_path = "${path.module}/dist/lambdas/dispatcher.zip"

  excludes = [
    "__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "tests",
    "tests/**",
    "README.md",
  ]
}

data "archive_file" "read_invoice_lambda_code" {
  type        = "zip"
  source_dir  = "${path.module}/lambdas/read_invoice"
  output_path = "${path.module}/dist/lambdas/read_invoice.zip"

  excludes = [
    "__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "tests",
    "tests/**",
    "README.md",
  ]
}

data "archive_file" "find_open_orders_lambda_code" {
  type        = "zip"
  source_dir  = "${path.module}/lambdas/find_open_orders"
  output_path = "${path.module}/dist/lambdas/find_open_orders.zip"

  excludes = [
    "__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "tests",
    "tests/**",
    "README.md",
  ]
}

data "archive_file" "propose_match_lambda_code" {
  type        = "zip"
  source_dir  = "${path.module}/lambdas/propose_match"
  output_path = "${path.module}/dist/lambdas/propose_match.zip"

  excludes = [
    "__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "tests",
    "tests/**",
    "README.md",
  ]
}

data "archive_file" "decision_lambda_code" {
  type        = "zip"
  source_dir  = "${path.module}/lambdas/decision"
  output_path = "${path.module}/dist/lambdas/decision.zip"

  excludes = [
    "__pycache__",
    "**/__pycache__/**",
    "**/*.pyc",
    "tests",
    "tests/**",
    "README.md",
  ]
}

data "aws_bedrock_foundation_models" "text_models" {
  by_output_modality = "TEXT"
}

data "aws_bedrock_inference_profile" "bedrock_inference_profile" {
  inference_profile_id = "${var.invoice_matcher_inference_profile_scope}.${try(local.invoice_matcher_model_ids[0], "none")}"

  lifecycle {
    precondition {
      condition     = length(local.invoice_matcher_model_ids) == 1
      error_message = "invoice_matcher_model_name \"${var.invoice_matcher_model_name}\" must match exactly one Bedrock text model; it matched ${length(local.invoice_matcher_model_ids)} (${join(", ", local.invoice_matcher_model_ids)}). Check the exact name with: aws bedrock list-foundation-models --by-output-modality TEXT --query \"modelSummaries[].modelName\""
    }
  }
}

data "aws_iam_policy_document" "invoice_matcher_assume_role_policy" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.caller_identity.account_id]
    }
  }
}

data "aws_iam_policy_document" "invoice_matcher_agent_policy" {
  statement {
    sid    = "InvokeModel"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
    ]
    resources = concat(
      [data.aws_bedrock_inference_profile.bedrock_inference_profile.inference_profile_arn],
      [for model in data.aws_bedrock_inference_profile.bedrock_inference_profile.models : model.model_arn],
    )
  }

  statement {
    sid       = "PullHarnessImage"
    effect    = "Allow"
    actions   = ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"]
    resources = ["*"]
  }

  statement {
    sid    = "Tracing"
    effect = "Allow"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "LogsGroup"
    effect    = "Allow"
    actions   = ["logs:CreateLogGroup", "logs:DescribeLogStreams"]
    resources = ["arn:aws:logs:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*"]
  }

  statement {
    sid       = "LogsDescribeGroups"
    effect    = "Allow"
    actions   = ["logs:DescribeLogGroups"]
    resources = ["arn:aws:logs:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:log-group:*"]
  }

  statement {
    sid       = "LogsStream"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["arn:aws:logs:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*"]
  }

  statement {
    sid       = "Metrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }

  statement {
    sid    = "WorkloadIdentity"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:workload-identity-directory/default",
      "arn:aws:bedrock-agentcore:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:workload-identity-directory/default/workload-identity/${var.invoice_matcher_harness_name}-*",
      "arn:aws:bedrock-agentcore:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:workload-identity-directory/default/workload-identity/harness_${var.invoice_matcher_harness_name}-*",
    ]
  }

  statement {
    sid       = "ApplyGuardrail"
    effect    = "Allow"
    actions   = ["bedrock:ApplyGuardrail"]
    resources = [aws_bedrock_guardrail.invoice_matcher_guardrail.guardrail_arn]
  }

  statement {
    sid    = "DeliverSpansToAgentLogGroup"
    effect = "Allow"
    actions = [
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "InvokeToolsGateway"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeGateway"]
    resources = [aws_bedrockagentcore_gateway.tools_gateway.gateway_arn]
  }
}

data "aws_iam_policy_document" "tools_gateway_assume_role_policy" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.caller_identity.account_id]
    }
  }
}

data "aws_iam_policy_document" "tools_gateway_policy" {
  statement {
    sid     = "InvokeToolLambdas"
    effect  = "Allow"
    actions = ["lambda:InvokeFunction"]
    resources = [
      aws_lambda_function.lambda_function_read_invoice.arn,
      aws_lambda_function.lambda_function_find_open_orders.arn,
      aws_lambda_function.lambda_function_propose_match.arn,
    ]
  }
}

data "aws_iam_policy_document" "xray_transaction_search_policy" {
  statement {
    sid    = "TransactionSearchXRayAccess"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["xray.amazonaws.com"]
    }

    actions = [
      "logs:PutLogEvents",
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
    ]

    resources = [
      "arn:aws:logs:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:log-group:aws/spans:*",
      "arn:aws:logs:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:log-group:/aws/application-signals/data:*",
    ]

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:xray:${data.aws_region.region.region}:${data.aws_caller_identity.caller_identity.account_id}:*"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.caller_identity.account_id]
    }
  }
}

data "aws_route53_zone" "route53_zone" {
  name = var.domain
}