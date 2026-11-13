# The pipeline as a Fargate task: the Docker image from ECR, run on a schedule, with the S3
# backend switched on through environment variables (no credentials: the task role supplies them).

resource "aws_ecs_cluster" "main" {
  name = local.name

  setting {
    name  = "containerInsights"
    value = "disabled" # extra metrics cost money; the logs and alerts are enough here
  }
}

resource "aws_security_group" "task" {
  name        = "${local.name}-task"
  description = "Pipeline task: no inbound traffic; outbound HTTPS only (ECR, S3, CloudWatch)"
  vpc_id      = data.aws_vpc.default.id

  egress {
    description = "HTTPS to AWS APIs"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_ecs_task_definition" "pipeline" {
  family                   = "${local.name}-pipeline"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "pipeline"
    image     = "${aws_ecr_repository.pipeline.repository_url}:${var.image_tag}"
    essential = true
    environment = [
      { name = "RISKPLATFORM_STORAGE_BACKEND", value = "s3" },
      { name = "RISKPLATFORM_S3_BUCKET", value = aws_s3_bucket.results.bucket },
      { name = "AWS_DEFAULT_REGION", value = var.region },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.pipeline.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "pipeline"
      }
    }
  }])
}
