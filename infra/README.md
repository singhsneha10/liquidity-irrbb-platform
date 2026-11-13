# Deploying to AWS (Phase 7)

The pipeline runs in AWS as a **scheduled Fargate task**: the Docker image from ECR, started on
weekday mornings by EventBridge Scheduler. It publishes results to a private S3 bucket and logs
to CloudWatch. You get an email if a run does not end cleanly.

```
EventBridge Scheduler (weekdays 06:00 London, off until you turn it on)
   -> ECS Fargate task (the riskplatform image from ECR, riskplatform-run)
        -> S3: s3://<bucket>/riskplatform/... (runs under their id, latest.json last)
        -> CloudWatch Logs: /riskplatform/pipeline
   -> exit code 1 or 2, or the task fails to start -> SNS email
AWS Budget -> email at 50%, 80%, 100% of actual spend and 100% of forecast
```

**You run every command that creates or changes AWS resources.** Claude writes and checks the
code (`terraform fmt`, `terraform validate` and CI) but never applies it.

## Cost

| Item | Roughly |
|---|---|
| Fargate, 0.5 vCPU / 1 GB, a few minutes each weekday | well under 1 USD a month |
| S3 (small files, old versions expire after 30 days) and CloudWatch Logs (90 days) | cents |
| ECR (last 10 images kept) | cents |
| Public IPv4 address while a task runs | cents |
| NAT gateway | **none**: the default VPC with a public IP avoids it (about 35 USD a month) |

The budget defaults to **10 USD a month** (`monthly_budget_usd`).

## 0. One-time setup on your PC

1. Install the tools, then **restart the terminal**:
   ```
   winget install Hashicorp.Terraform
   winget install Amazon.AWSCLI
   winget install Docker.DockerDesktop      # needs a Windows restart the first time
   ```
2. In the AWS console, signed in as the account's **root user**:
   - turn on **MFA for the root user**, and then stop using root for day-to-day work;
   - set up **IAM Identity Center** and create a user for yourself with the
     `AdministratorAccess` permission set (Terraform needs to create roles). This gives
     short-lived sign-ins instead of long-lived access keys.
3. Sign in from the terminal and use that profile for everything below:
   ```
   aws configure sso --profile riskplatform      # region eu-west-2
   aws sso login --profile riskplatform
   $env:AWS_PROFILE = "riskplatform"
   aws sts get-caller-identity                    # shows your account id: you are signed in
   ```

Never put access keys in this repository, in `terraform.tfvars` or in the image.

## 1. The billing alarm (first, before anything else)

```
cd infra/terraform/billing
Copy-Item terraform.tfvars.example terraform.tfvars    # then put your email in it
terraform init
terraform plan        # read it: it should create exactly one aws_budgets_budget
terraform apply
```

## 2. The platform

```
cd ../platform
Copy-Item terraform.tfvars.example terraform.tfvars    # your email; schedule_enabled = false
terraform init
terraform plan        # read it: bucket, ECR, log group, 3 IAM roles, ECS, schedule, alerts
terraform apply
terraform output      # bucket, ecr_repository_url, cluster, task_definition, ...
```

AWS emails you to **confirm the SNS alert subscription**. Click the link, or no alerts arrive.

## 3. Build and push the image

From the repository root (the tag must equal `image_tag`, and tags cannot be reused):

```
$repo = terraform -chdir=infra/terraform/platform output -raw ecr_repository_url
$registry = $repo.Split("/")[0]
aws ecr get-login-password --region eu-west-2 | docker login --username AWS --password-stdin $registry
docker build -t "${repo}:0.1.0" .
docker push "${repo}:0.1.0"
```

## 4. Run it once by hand and check it

```
$tf = "infra/terraform/platform"
terraform -chdir=$tf output -raw run_task_network | Out-File -Encoding ascii network.json
aws ecs run-task --cluster (terraform -chdir=$tf output -raw cluster) `
  --launch-type FARGATE `
  --task-definition (terraform -chdir=$tf output -raw task_definition) `
  --network-configuration file://network.json
aws logs tail /riskplatform/pipeline --follow       # the run's log as it happens
$bucket = terraform -chdir=$tf output -raw bucket
aws s3 cp "s3://$bucket/riskplatform/latest.json" -  # the run that just published
Remove-Item network.json
```

The synthetic bank's BANK_UK is an IRRBB outlier, so expect **exit code 2** (findings) and an
alert email. That is the pipeline working as designed.

## 5. Turn the schedule on

Set `schedule_enabled = true` in `infra/terraform/platform/terraform.tfvars`, then run
`terraform apply` again.

## Releasing a new version

Choose a new tag (for example `0.1.1`), build and push it as in step 3, set `image_tag` to it in
`terraform.tfvars`, and run `terraform apply`. Old images stay in ECR (the last 10).

## Removing everything

```
$bucket = terraform -chdir=infra/terraform/platform output -raw bucket
aws s3 rm "s3://$bucket" --recursive        # the bucket must be empty before it can be deleted
# Versioned buckets also keep old versions: empty it in the console ("Empty" button) if
# destroy reports BucketNotEmpty.
terraform -chdir=infra/terraform/platform destroy
terraform -chdir=infra/terraform/billing destroy    # last, so the alarm outlives everything
```

## Notes

- **State:** Terraform keeps what it created in `terraform.tfstate` in each folder. It is ignored
  by Git; keep it safe (back it up), or Terraform loses track of the resources. A remote state
  backend (S3 with locking) is the next step for a team.
- **Network:** the default VPC, a public IP and an outbound-only (HTTPS) security group: nothing
  can connect in. Private subnets with VPC endpoints would remove the public IP at extra cost.
- **Least privilege:** the task may only read and write its own bucket prefix; the scheduler may
  only start this task. `tests/test_infra.py` checks there are no wildcard IAM actions.
- **Dagster** stays the local orchestration and monitoring layer (`dagster dev`); running its
  daemon and web server in AWS would cost money around the clock.
