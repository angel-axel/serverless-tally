variable "spa_bucket_name" {
  description = "Prefix for the S3 bucket that hosts the SPA. The account ID, region and '-an' suffix are appended to build the account regional namespace name."
  type        = string
  default     = "tally-spa-bucket"
}

variable "bucket_name" {
  description = "Prefix for the S3 bucket that stores the raw inbound emails received by SES. The account ID, region and '-an' suffix are appended to build the account regional namespace name."
  type        = string
  default     = "tally-bucket"
}

variable "domain" {
  description = "Apex domain of the Route 53 public hosted zone (e.g. example.com)."
  type        = string
}

variable "subdomain" {
  description = "Subdomain where the site is served (e.g. 'other' for other.example.com). Also used for the ACM certificate and the CloudFront alias."
  type        = string
  default     = ""
}

variable "acm_dns_records_ttl" {
  description = "TTL in seconds for the ACM DNS validation records."
  type        = number
  default     = 600
}

variable "cloudfront_default_ttl" {
  description = "Seconds CloudFront caches an object when the origin sends no Cache-Control or Expires header."
  type        = number
  default     = 3600
}

variable "cloudfront_min_ttl" {
  description = "Minimum seconds CloudFront caches an object, even if the origin asks for less."
  type        = number
  default     = 0
}

variable "cloudfront_max_ttl" {
  description = "Maximum seconds CloudFront caches an object, even if the origin asks for more."
  type        = number
  default     = 86400
}

variable "ses_dns_record_txt_ttl" {
  description = "TTL in seconds for the TXT record (_amazonses) that verifies the domain identity in SES."
  type        = number
  default     = 600
}

variable "ses_dns_record_dkim_ttl" {
  description = "TTL in seconds for the three CNAME records that enable DKIM signing for the SES domain identity."
  type        = number
  default     = 600
}

variable "ses_dns_record_mx_ttl" {
  description = "TTL in seconds for the MX record that routes inbound email for the domain to the SES receiving endpoint."
  type        = number
  default     = 600
}

variable "ses_rule_set_name" {
  description = "Name of the SES receipt rule set. It becomes the active rule set for the region, replacing any rule set that was active before. Must match the SourceArn condition in the emails bucket policy."
  type        = string
  default     = "tally-rule-set"
}

variable "ses_rule_name" {
  description = "Name of the SES receipt rule that stores inbound emails in the emails bucket. Must match the SourceArn condition in the emails bucket policy."
  type        = string
  default     = "store-in-s3"
}

variable "ses_recipient_local_part" {
  description = "Local part of the inbound email address (the part before the @). Emails sent to <local_part>@<domain> are stored in the emails bucket."
  type        = string
  default     = "invoices"
}

variable "invoices_table_name" {
  description = "Name of the DynamoDB table that tracks every invoice (CFDI) and its status."
  type        = string
  default     = "tally-invoices"
}

variable "purchase_orders_table_name" {
  description = "Name of the DynamoDB table that stores the purchase orders sent to suppliers, used to match incoming invoices."
  type        = string
  default     = "tally-purchase-orders"
}

variable "gatekeeper_function_name" {
  description = "Name of the Lambda function that inspects inbound emails and extracts CFDI attachments."
  type        = string
  default     = "tally-gatekeeper"
}

variable "gatekeeper_log_retention_days" {
  description = "Days to keep the gatekeeper Lambda logs in CloudWatch Logs."
  type        = number
  default     = 14
}

variable "dispatcher_function_name" {
  description = "Name of the Lambda function that invokes the invoice-matching agent for every new invoice."
  type        = string
  default     = "tally-dispatcher"
}

variable "dispatcher_log_retention_days" {
  description = "Days to keep the dispatcher Lambda logs in CloudWatch Logs."
  type        = number
  default     = 14
}

variable "read_invoice_function_name" {
  description = "Name of the Lambda function behind the agent's read_invoice tool."
  type        = string
  default     = "tally-read-invoice"
}

variable "find_open_orders_function_name" {
  description = "Name of the Lambda function behind the agent's find_open_orders tool."
  type        = string
  default     = "tally-find-open-orders"
}

variable "propose_match_function_name" {
  description = "Name of the Lambda function behind the agent's propose_match tool."
  type        = string
  default     = "tally-propose-match"
}

variable "tools_log_retention_days" {
  description = "Days to keep the logs of the agent tool Lambdas in CloudWatch Logs."
  type        = number
  default     = 14
}

variable "decision_function_name" {
  description = "Name of the Lambda function that checks the agent's proposal and decides whether the invoice matches its purchase order."
  type        = string
  default     = "tally-decision"
}

variable "decision_log_retention_days" {
  description = "Days to keep the decision Lambda logs in CloudWatch Logs."
  type        = number
  default     = 14
}

variable "decision_price_tolerance" {
  description = "Maximum difference, in the invoice currency, allowed between the invoiced unit price and the purchase order unit price (for rounding). Use \"0\" for an exact match."
  type        = string
  default     = "0.01"

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,2})?$", var.decision_price_tolerance))
    error_message = "Use a non-negative amount with up to two decimals, for example \"0.01\"."
  }
}

variable "decision_max_auto_match_total" {
  description = "Invoices with a total above this amount always go to needs_review, even when every check passes."
  type        = string
  default     = "50000"

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]{1,2})?$", var.decision_max_auto_match_total))
    error_message = "Use a non-negative amount with up to two decimals, for example \"50000\"."
  }
}

variable "agent_name" {
  description = "Name of the invoice matcher agent, used to name its IAM role and policy (\"<agent_name>-role\", \"<agent_name>-permissions\"), like the Lambda functions. Must start with \"tally-\", the prefix the CodeBuild deployment policy allows for roles."
  type        = string
  default     = "tally-invoice-matcher"
}

variable "invoice_matcher_harness_name" {
  description = "Name of the AgentCore harness that matches invoices against purchase orders. Letters, numbers and underscores only (no hyphens), up to 40 characters."
  type        = string
  default     = "tally_invoice_matcher"

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,39}$", var.invoice_matcher_harness_name))
    error_message = "Use only letters, numbers and underscores, start with a letter, and keep it to 40 characters."
  }
}

variable "invoice_matcher_model_name" {
  description = "Bedrock model name for the invoice matcher agent, exactly as Bedrock lists it (for example \"Claude Sonnet 4.6\"). List the available names with: aws bedrock list-foundation-models --by-output-modality TEXT --query \"modelSummaries[].modelName\""
  type        = string
  nullable    = false

  validation {
    condition     = trimspace(var.invoice_matcher_model_name) != ""
    error_message = "Set invoice_matcher_model_name to a Bedrock model name, for example \"Claude Sonnet 4.6\"."
  }
}

variable "invoice_matcher_inference_profile_scope" {
  description = "Geography of the system-defined cross-region inference profile used to call the model: \"global\" routes to any commercial region, \"us\" keeps requests in US regions, and so on. The profile ID is \"<scope>.<model ID>\"."
  type        = string
  default     = "global"

  validation {
    condition     = contains(["global", "us", "eu", "apac", "jp", "au", "ca", "us-gov"], var.invoice_matcher_inference_profile_scope)
    error_message = "Use one of: global, us, eu, apac, jp, au, ca, us-gov."
  }
}

variable "agent_max_iterations" {
  description = "Maximum number of agent loop iterations per invoice. Caps cost and stops runaway tool loops."
  type        = number
  default     = 10
}

variable "agent_timeout_seconds" {
  description = "Maximum duration in seconds of one agent run. When the dispatcher is added, its Lambda timeout is set to this value plus 60 seconds."
  type        = number
  default     = 120
}

variable "tools_gateway_name" {
  description = "Name of the AgentCore Gateway that exposes the agent's tools, also used to name its IAM role and policy. Must start with \"tally-\", the prefix the CodeBuild deployment policy allows for roles and gateways."
  type        = string
  default     = "tally-tools"

  validation {
    condition     = can(regex("^tally-[a-z0-9-]{1,40}$", var.tools_gateway_name))
    error_message = "Use \"tally-\" followed by lowercase letters, numbers or hyphens."
  }
}

variable "failed_events_queue_name" {
  description = "Name of the SQS queue that keeps every event that could not be delivered or processed: stream records the pipe could not publish, events a rule could not deliver, and Lambda invocations that failed after their retries."
  type        = string
  default     = "tally-failed-events"

  validation {
    condition     = can(regex("^tally-[a-z0-9-]{1,60}$", var.failed_events_queue_name))
    error_message = "The queue name must start with 'tally-' and use lowercase letters, digits and hyphens."
  }
}

variable "invoice_events_bus_name" {
  description = "Name of the EventBridge bus that receives every change to the invoices table and routes it to the Lambdas that react to it."
  type        = string
  default     = "tally-invoice-events"

  validation {
    condition     = can(regex("^tally-[a-z0-9-]{1,60}$", var.invoice_events_bus_name))
    error_message = "The bus name must start with 'tally-' and use lowercase letters, digits and hyphens."
  }
}

variable "invoice_events_source" {
  description = "Value of the 'source' field of the events the pipe publishes on the invoice events bus. Every rule matches on it."
  type        = string
  default     = "tally.invoices"
}

variable "invoices_stream_pipe_name" {
  description = "Name of the EventBridge pipe that is the only reader of the invoices table stream."
  type        = string
  default     = "tally-invoices-stream"

  validation {
    condition     = can(regex("^tally-[a-z0-9-]{1,50}$", var.invoices_stream_pipe_name))
    error_message = "The pipe name must start with 'tally-' and use lowercase letters, digits and hyphens."
  }
}

variable "review_notifier_function_name" {
  description = "Name of the Lambda function that emails an invoice to the reviewer when it is sent to review."
  type        = string
  default     = "tally-review-notifier"
}

variable "review_notifier_log_retention_days" {
  description = "Days to keep the review notifier Lambda logs in CloudWatch."
  type        = number
  default     = 14
}

variable "review_recipient_email" {
  description = "Email address that receives the invoices sent to review. While SES is in the sandbox, it must click the verification link SES sends once."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.review_recipient_email))
    error_message = "The review recipient must be a valid email address."
  }
}

variable "review_sender_local_part" {
  description = "Local part of the address review emails are sent from (the part before the @). The domain is the one already verified in SES."
  type        = string
  default     = "review"
}

