variable "spa_bucket_name" {
  description = "Prefix for the S3 bucket that hosts the SPA. The account ID, region and '-an' suffix are appended to build the account regional namespace name."
  type        = string
  default     = "tally-spa-bucket"
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

variable "dns_records_ttl" {
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