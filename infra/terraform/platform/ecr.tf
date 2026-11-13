# Image registry. Tags are immutable (a released image can never be silently replaced), images
# are scanned for known vulnerabilities on push, and only the last 10 are kept.

resource "aws_ecr_repository" "pipeline" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "pipeline" {
  repository = aws_ecr_repository.pipeline.name

  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the last 10 images"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 10
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_cloudwatch_log_group" "pipeline" {
  name              = "/${local.name}/pipeline"
  retention_in_days = var.log_retention_days
}
