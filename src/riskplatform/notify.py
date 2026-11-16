"""Limit alert emails: one message per run listing the internal limits at warning or breach.

After the limits stage, if the run's limits summary (data/limits/<run_id>/summary.json) has
alerts (warnings and breaches on the latest date), ONE email is published to the SNS topic that
already carries the failure alerts (infra/terraform/platform/alerts.tf); its subscribers, your
email address, receive it.

- The topic comes only from the RISKPLATFORM_ALERT_TOPIC_ARN environment variable, which
  Terraform sets on the AWS task. Without it (locally, in tests and in CI) nothing is sent and
  the alerts stay in the log, as before.
- No credentials in code: boto3 finds them (in AWS, the task's IAM role, which may publish to
  this one topic only). The region is read from the topic ARN.
- A run with no alerts sends nothing. A failure to send raises, so the run exits 1 and the
  failure alert (any exit code except 0 and 2) tells you: a limit alert is never lost silently.

The email repeats every run while a limit stays at warning or breach (labelled choice: a
"what changed since yesterday" view comes in step 8.4).
"""

import json
import logging
import os
from pathlib import Path

import boto3

from riskplatform.limits import describe_alert

logger = logging.getLogger(__name__)

ALERT_TOPIC_ENV = "RISKPLATFORM_ALERT_TOPIC_ARN"
SUBJECT_MAX = 100  # SNS email subjects: one line, at most 100 characters


def _count(number: int, word: str, plural: str) -> str:
    return f"{number} {word if number == 1 else plural}"


def compose_limit_email(summary: dict) -> tuple[str, str]:
    """The subject and body of the limit alert email for one run's limits summary."""
    alerts = summary["alerts"]
    breaches = sum(alert["status"] == "breach" for alert in alerts)
    warnings = len(alerts) - breaches
    subject = (
        f"riskplatform limits {summary['as_of']}: "
        f"{_count(breaches, 'breach', 'breaches')}, {_count(warnings, 'warning', 'warnings')}"
    )
    thresholds = summary["thresholds"]
    run_id = summary["source_run_id"]
    levels = (
        f"Warning from {thresholds['warning']:.0%} of a limit used; "
        f"breach from {thresholds['breach']:.0%}."
    )
    lines = [
        f"Internal limits at warning or breach on {summary['as_of']} (run {run_id}):",
        "",
        *(f"- {describe_alert(alert)}" for alert in alerts),  # in the stage's order: breaches first
        "",
        levels,
        "These are the bank's own limits (config/limits.yaml), not the regulatory minimums.",
        f"Details: limits/{run_id}/summary.json (in data/ and, in AWS, in the results bucket).",
        "Sent once per run while any limit is at warning or breach. All positions are synthetic.",
    ]
    return subject[:SUBJECT_MAX], "\n".join(lines)


def region_of(topic_arn: str) -> str:
    """The region in an SNS topic ARN (arn:aws:sns:<region>:<account>:<name>)."""
    parts = topic_arn.split(":")
    if len(parts) != 6 or parts[:3] != ["arn", "aws", "sns"] or not parts[3]:
        raise ValueError(f"{ALERT_TOPIC_ENV} is not an SNS topic ARN: {topic_arn!r}")
    return parts[3]


def send_limit_alerts(summary: dict, topic_arn: str | None, client=None) -> bool:
    """Publish one email if the summary has alerts and a topic is set; True if sent."""
    alerts = summary["alerts"]
    if not alerts:
        return False
    if not topic_arn:
        logger.info("%d limit alerts not emailed: %s is not set", len(alerts), ALERT_TOPIC_ENV)
        return False
    subject, body = compose_limit_email(summary)
    client = client or boto3.client("sns", region_name=region_of(topic_arn))
    client.publish(TopicArn=topic_arn, Subject=subject, Message=body)
    logger.info("Emailed %d limit alerts (%s) to %s", len(alerts), subject, topic_arn)
    return True


def notify_run(data_dir: str | Path, run_id: str, env: dict[str, str] | None = None) -> bool:
    """Send the limit alert email for one run's limits summary (see send_limit_alerts)."""
    env = dict(os.environ) if env is None else env
    path = Path(data_dir) / "limits" / run_id / "summary.json"
    summary = json.loads(path.read_text(encoding="utf-8"))
    return send_limit_alerts(summary, env.get(ALERT_TOPIC_ENV))
