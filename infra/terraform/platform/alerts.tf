# Email alerts when a run fails. The pipeline's exit codes reach ECS:
#   0 = clean, 1 = stopped or error, 2 = findings (a breach or an IRRBB outlier).
# Alerts go out for every exit code except 0 and 2: that is 1, and also crash codes the program
# never sees, such as 137 (killed for running out of memory). Findings (2) send no email: they
# are in the logs, S3 and latest.json, and become limit alerts in Phase 8.
# A task that never starts (e.g. the image is missing) has no exit code, so it has its own rule.
# The first email from SNS asks you to confirm the subscription.

resource "aws_sns_topic" "alerts" {
  name = "${local.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_event_rule" "run_failed" {
  name        = "${local.name}-run-failed"
  description = "A pipeline run failed: any exit code except 0 (clean) and 2 (findings)"

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.main.arn]
      lastStatus = ["STOPPED"]
      containers = { exitCode = [{ "anything-but" = [0, 2] }] }
    }
  })
}

resource "aws_cloudwatch_event_rule" "run_failed_to_start" {
  name        = "${local.name}-run-failed-to-start"
  description = "A pipeline task could not start (e.g. the image is missing)"

  event_pattern = jsonencode({
    source        = ["aws.ecs"]
    "detail-type" = ["ECS Task State Change"]
    detail = {
      clusterArn = [aws_ecs_cluster.main.arn]
      lastStatus = ["STOPPED"]
      stopCode   = ["TaskFailedToStart"]
    }
  })
}

resource "aws_cloudwatch_event_target" "run_failed" {
  rule = aws_cloudwatch_event_rule.run_failed.name
  arn  = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      code   = "$.detail.containers[0].exitCode"
      reason = "$.detail.stoppedReason"
      task   = "$.detail.taskArn"
    }
    input_template = "\"riskplatform run FAILED with exit code <code> (1 = stopped by a check or an error; 137 = out of memory). latest.json still names the last good run. Reason: <reason>. Task: <task>. Logs: CloudWatch /riskplatform/pipeline.\""
  }
}

resource "aws_cloudwatch_event_target" "run_failed_to_start" {
  rule = aws_cloudwatch_event_rule.run_failed_to_start.name
  arn  = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      reason = "$.detail.stoppedReason"
      task   = "$.detail.taskArn"
    }
    input_template = "\"riskplatform run could not start. Reason: <reason>. Task: <task>.\""
  }
}

data "aws_iam_policy_document" "alerts" {
  statement {
    sid       = "EventBridgeMayPublish"
    actions   = ["sns:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values = [
        aws_cloudwatch_event_rule.run_failed.arn,
        aws_cloudwatch_event_rule.run_failed_to_start.arn,
      ]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts.json
}
