output "bucket" {
  description = "Results bucket (pass to the container as RISKPLATFORM_S3_BUCKET; Terraform already does)."
  value       = aws_s3_bucket.results.bucket
}

output "ecr_repository_url" {
  description = "Push the pipeline image here, tagged with var.image_tag."
  value       = aws_ecr_repository.pipeline.repository_url
}

output "cluster" {
  description = "ECS cluster that runs the pipeline task."
  value       = aws_ecs_cluster.main.name
}

output "task_definition" {
  description = "The pipeline task definition (family:revision)."
  value       = "${aws_ecs_task_definition.pipeline.family}:${aws_ecs_task_definition.pipeline.revision}"
}

output "log_group" {
  description = "CloudWatch log group with each run's log."
  value       = aws_cloudwatch_log_group.pipeline.name
}

output "schedule" {
  description = "The daily schedule (state follows var.schedule_enabled)."
  value       = "${aws_scheduler_schedule.daily.name} (${aws_scheduler_schedule.daily.state})"
}

output "run_task_network" {
  description = "Network settings for a manual run (aws ecs run-task --network-configuration ...)."
  value = jsonencode({
    awsvpcConfiguration = {
      subnets        = data.aws_subnets.default.ids
      securityGroups = [aws_security_group.task.id]
      assignPublicIp = "ENABLED"
    }
  })
}
