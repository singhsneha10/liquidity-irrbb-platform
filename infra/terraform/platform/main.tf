# Shared lookups and names.
#
# Network (labelled cost choice): the task runs in the account's default VPC with a public IP and
# an outbound-only security group. That avoids a NAT gateway (about 35 USD a month); nothing can
# connect in, and the task only calls AWS APIs over HTTPS.

data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }

  filter {
    name   = "default-for-az"
    values = ["true"]
  }
}

locals {
  name       = "riskplatform"
  account_id = data.aws_caller_identity.current.account_id
  # Bucket names are global; account id and region make this one unique.
  bucket_name = "${local.name}-${local.account_id}-${var.region}"
}
