variable "alert_email" {
  description = "Email address that receives budget alerts (set in terraform.tfvars, never committed)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "monthly_budget_usd" {
  description = "Monthly cost budget in US dollars; alerts fire at 50%, 80% and 100% of it."
  type        = number
  default     = 10

  validation {
    condition     = var.monthly_budget_usd > 0
    error_message = "monthly_budget_usd must be above 0."
  }
}

variable "region" {
  description = "AWS region for the provider (budgets themselves are account-wide)."
  type        = string
  default     = "eu-west-2"
}
