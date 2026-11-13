variable "region" {
  description = "AWS region. Must equal s3.region in config/storage.yaml (a test checks)."
  type        = string
  default     = "eu-west-2"
}

variable "alert_email" {
  description = "Email that receives run alerts (set in terraform.tfvars, never committed)."
  type        = string

  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "image_tag" {
  description = "Tag of the pipeline image in ECR. Tags are immutable, so use a new one per release."
  type        = string
  default     = "0.1.0"
}

variable "s3_prefix" {
  description = "Key prefix in the bucket. Must equal s3.prefix in config/storage.yaml (a test checks)."
  type        = string
  default     = "riskplatform"
}

variable "task_cpu" {
  description = "Fargate CPU units for the pipeline task (512 = 0.5 vCPU)."
  type        = number
  default     = 512
}

variable "task_memory" {
  description = "Fargate memory for the pipeline task, in MiB."
  type        = number
  default     = 1024
}

variable "log_retention_days" {
  description = "How long CloudWatch keeps the pipeline logs."
  type        = number
  default     = 90
}

variable "schedule_expression" {
  description = "When the pipeline runs (EventBridge Scheduler cron): weekdays 06:00, as the Dagster schedule."
  type        = string
  default     = "cron(0 6 ? * MON-FRI *)"
}

variable "schedule_timezone" {
  description = "Time zone for the schedule."
  type        = string
  default     = "Europe/London"
}

variable "schedule_enabled" {
  description = "Whether the daily schedule runs. Off by default: turn it on after a first manual run."
  type        = bool
  default     = false
}
