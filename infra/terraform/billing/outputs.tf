output "budget_name" {
  description = "The account budget that sends the billing alerts."
  value       = aws_budgets_budget.monthly.name
}

output "monthly_budget_usd" {
  description = "The monthly budget in US dollars."
  value       = aws_budgets_budget.monthly.limit_amount
}
