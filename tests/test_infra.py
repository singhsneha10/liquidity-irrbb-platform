"""Safety rules for the AWS infrastructure code, checked without AWS (CI also runs
terraform fmt and terraform validate on it).

The billing alarm exists; the bucket is private, encrypted, versioned and HTTPS-only; IAM has no
wildcard actions; images are immutable; Terraform agrees with config/storage.yaml and with the
Dagster schedule; and no credentials or real variable files are in the repository.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

from riskplatform.orchestration.definitions import daily_schedule

ROOT = Path(__file__).resolve().parents[1]
BILLING = ROOT / "infra" / "terraform" / "billing"
PLATFORM = ROOT / "infra" / "terraform" / "platform"


def _read(folder: Path) -> str:
    return "\n".join(path.read_text(encoding="utf-8") for path in sorted(folder.glob("*.tf")))


def _default(name: str) -> str:
    """The default of a platform variable, as written."""
    block = re.search(rf'variable "{name}" \{{(.*?)\n\}}', _read(PLATFORM), re.DOTALL)
    assert block, name
    return re.search(r"default\s*=\s*(.+)", block.group(1)).group(1).strip().strip('"')


def test_billing_alarm_alerts_on_actual_and_forecast_spend():
    billing = _read(BILLING)
    assert 'resource "aws_budgets_budget" "monthly"' in billing
    assert "for_each = [50, 80, 100]" in billing
    assert 'notification_type          = "ACTUAL"' in billing
    assert 'notification_type          = "FORECASTED"' in billing
    assert "aws_budgets_budget" not in _read(PLATFORM)  # applied on its own, first


def test_results_bucket_is_private_encrypted_versioned_and_https_only():
    s3 = (PLATFORM / "s3.tf").read_text(encoding="utf-8")
    for setting in ("block_public_acls", "block_public_policy", "ignore_public_acls",
                    "restrict_public_buckets"):  # fmt: skip
        assert re.search(rf"{setting}\s*=\s*true", s3), setting
    assert 'sse_algorithm = "AES256"' in s3
    assert 'status = "Enabled"' in s3
    assert 'variable = "aws:SecureTransport"' in s3 and 'effect  = "Deny"' in s3


def _statements(text: str) -> list[str]:
    return re.findall(r"statement \{(.*?)\n  \}", text, re.DOTALL)


def test_iam_has_no_wildcard_actions_and_only_one_wildcard_resource():
    iam = (PLATFORM / "iam.tf").read_text(encoding="utf-8")
    statements = _statements(iam)
    assert len(statements) >= 6
    for statement in statements:
        actions = re.findall(r'"([a-z0-9-]+:[A-Za-z*]+)"', statement.split("resources")[0])
        assert all("*" not in action for action in actions), actions
        if 'resources = ["*"]' in statement:
            assert actions == ["ecr:GetAuthorizationToken"], actions


def test_alerts_email_on_failures_but_not_on_findings():
    """Exit 1 (stopped or error) and crash codes such as 137 (out of memory) email; 0 (clean)
    and 2 (findings: they wait for the Phase 8 limit alerts) do not."""
    alerts = (PLATFORM / "alerts.tf").read_text(encoding="utf-8")
    rule = re.search(
        r'resource "aws_cloudwatch_event_rule" "run_failed" \{(.*?)\n\}', alerts, re.DOTALL
    )
    assert rule, "the run_failed rule is missing"
    silent = re.search(r'exitCode = \[\{ "anything-but" = \[([\d, ]+)\] \}\]', rule.group(1))
    assert silent, "run_failed must match exit codes with anything-but [...]"
    no_email = {int(code) for code in silent.group(1).split(",")}

    def emails(exit_code: int) -> bool:  # what EventBridge's anything-but does
        return exit_code not in no_email

    assert not emails(0) and not emails(2)
    assert emails(1) and emails(137) and emails(143)
    assert 'stopCode   = ["TaskFailedToStart"]' in alerts  # no exit code at all: still alerts


def test_the_daily_task_runs_up_to_its_own_date():
    """The Fargate task passes --as-of today to the image's entry point (riskplatform-run)."""
    ecs = (PLATFORM / "ecs.tf").read_text(encoding="utf-8")
    assert re.search(r'command\s*=\s*\["--as-of", "today"\]', ecs)


def test_images_are_immutable_and_scanned():
    ecr = (PLATFORM / "ecr.tf").read_text(encoding="utf-8")
    assert 'image_tag_mutability = "IMMUTABLE"' in ecr
    assert "scan_on_push = true" in ecr


def test_terraform_agrees_with_the_storage_config():
    storage = yaml.safe_load((ROOT / "config" / "storage.yaml").read_text(encoding="utf-8"))
    assert _default("s3_prefix") == storage["s3"]["prefix"]
    assert _default("region") == storage["s3"]["region"]
    assert "RISKPLATFORM_STORAGE_BACKEND" in (PLATFORM / "ecs.tf").read_text(encoding="utf-8")


def test_cloud_schedule_matches_the_dagster_schedule_and_starts_off():
    minute, hour, *_ = daily_schedule.cron_schedule.split()  # "0 6 * * 1-5"
    assert daily_schedule.cron_schedule.endswith("* * 1-5")  # weekdays
    assert _default("schedule_expression") == f"cron({minute} {hour} ? * MON-FRI *)"
    assert _default("schedule_timezone") == daily_schedule.execution_timezone
    assert _default("schedule_enabled") == "false"


def _tracked_and_new_files() -> list[Path]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()  # fmt: skip
    return [ROOT / name for name in listed]


def test_no_aws_access_keys_anywhere_in_the_repository():
    key = re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")
    offenders = []
    for path in _tracked_and_new_files():
        try:
            if key.search(path.read_text(encoding="utf-8")):
                offenders.append(path)
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
    assert offenders == []


@pytest.mark.parametrize("folder", [BILLING, PLATFORM])
def test_only_example_variable_files_are_committed(folder):
    committable = {path.name for path in _tracked_and_new_files() if path.parent == folder}
    assert "terraform.tfvars.example" in committable
    assert not any(name.endswith(".tfvars") or ".tfstate" in name for name in committable)
    assert ".terraform.lock.hcl" in committable  # provider versions are pinned
