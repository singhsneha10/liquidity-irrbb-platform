# The daily run: EventBridge Scheduler starts the Fargate task (off until schedule_enabled = true).
# No retries: a stopped run means its data failed a check, and rerunning would not change that.

resource "aws_scheduler_schedule" "daily" {
  name                         = "${local.name}-daily"
  description                  = "Run the risk pipeline (riskplatform-run) on weekday mornings"
  schedule_expression          = var.schedule_expression
  schedule_expression_timezone = var.schedule_timezone
  state                        = var.schedule_enabled ? "ENABLED" : "DISABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_ecs_cluster.main.arn
    role_arn = aws_iam_role.scheduler.arn

    ecs_parameters {
      task_definition_arn = aws_ecs_task_definition.pipeline.arn
      launch_type         = "FARGATE"
      task_count          = 1

      network_configuration {
        subnets          = data.aws_subnets.default.ids
        security_groups  = [aws_security_group.task.id]
        assign_public_ip = true
      }
    }

    retry_policy {
      maximum_retry_attempts = 0
    }
  }
}
