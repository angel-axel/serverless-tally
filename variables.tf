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