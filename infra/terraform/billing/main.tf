# The billing alarm, applied BEFORE any other resource exists (infra/README.md, step 1).
#
# An AWS Budget for the whole account. It emails alert_email when actual spend this month passes
# 50%, 80% and 100% of the budget, and when AWS forecasts the month will pass 100%. The first
# email asks you to confirm nothing: budget emails need no subscription.

resource "aws_budgets_budget" "monthly" {
  name         = "riskplatform-monthly-cost"
  budget_type  = "COST"
  limit_amount = format("%.2f", var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [50, 80, 100]
    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.alert_email]
    }
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
  }
}
