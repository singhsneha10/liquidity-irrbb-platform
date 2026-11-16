"""Limit alert emails: composing, when nothing is sent, and publishing (moto's fake SNS).

Messages go to a fake SQS queue subscribed to the topic, so the tests read back exactly what an
email subscriber would receive. No AWS account, no cost, no real credentials.
"""

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from riskplatform import notify
from riskplatform.limits import describe_alert
from riskplatform.notify import (
    ALERT_TOPIC_ENV,
    compose_limit_email,
    notify_run,
    region_of,
    send_limit_alerts,
)
from riskplatform.pipeline import main

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"
REGION = "eu-west-2"


def _alert(status, entity, utilisation, limit="eve_tier1", regime="basel"):
    return {
        "date": "2026-11-13",
        "level": "entity" if entity else "group",
        "entity": entity,
        "currency": "GBP",
        "regime": regime,
        "limit": limit,
        "kind": "cap",
        "actual": 0.15 * utilisation,
        "limit_value": 0.15,
        "utilisation": utilisation,
        "status": status,
    }


def _summary(alerts) -> dict:
    return {
        "source_run_id": "20261113T060000Z-abc123",
        "as_of": "2026-11-13",
        "thresholds": {"warning": 0.9, "breach": 1.0},
        "alerts": alerts,
    }


ALERTS = [
    _alert("breach", "BANK_UK", 1.38),
    _alert("breach", "BANK_US", 1.01),
    _alert("warning", None, 0.97),
]


class _MustNotBeCalled:
    def publish(self, **kwargs):
        raise AssertionError("SNS publish should not have been called")


# --- Composing ---------------------------------------------------------------------------------


def test_subject_counts_breaches_and_warnings():
    subject, _ = compose_limit_email(_summary(ALERTS))
    assert subject == "riskplatform limits 2026-11-13: 2 breaches, 1 warning"
    one, _ = compose_limit_email(_summary([_alert("breach", "BANK_UK", 1.2)]))
    assert one.endswith("1 breach, 0 warnings")


def test_subject_is_one_short_line():
    subject, _ = compose_limit_email(_summary(ALERTS * 50))
    assert len(subject) <= 100 and "\n" not in subject


def test_body_lists_every_alert_in_order_with_the_context():
    _, body = compose_limit_email(_summary(ALERTS))
    lines = [line[2:] for line in body.splitlines() if line.startswith("- ")]
    assert lines == [describe_alert(alert) for alert in ALERTS]  # breaches first, as given
    assert "Warning from 90% of a limit used; breach from 100%." in body
    assert "not the regulatory minimums" in body
    assert "20261113T060000Z-abc123" in body


# --- When nothing is sent ----------------------------------------------------------------------


def test_no_alerts_sends_nothing():
    assert (
        send_limit_alerts(_summary([]), "arn:aws:sns:eu-west-2:123456789012:t", _MustNotBeCalled())
        is False
    )


def test_no_topic_sends_nothing_and_says_why(caplog):
    caplog.set_level("INFO", logger="riskplatform.notify")
    assert send_limit_alerts(_summary(ALERTS), None, _MustNotBeCalled()) is False
    assert f"3 limit alerts not emailed: {ALERT_TOPIC_ENV} is not set" in caplog.text


@pytest.mark.parametrize(
    "arn", ["", "not-an-arn", "arn:aws:s3:::bucket", "arn:aws:sns::123456789012:t"]
)
def test_a_malformed_topic_arn_is_refused(arn):
    with pytest.raises(ValueError, match=ALERT_TOPIC_ENV):
        region_of(arn)


def test_region_comes_from_the_topic_arn():
    assert region_of("arn:aws:sns:eu-west-2:123456789012:riskplatform-alerts") == REGION


# --- Publishing (moto) -------------------------------------------------------------------------


@pytest.fixture
def sns(monkeypatch):
    """A fake SNS topic with a fake SQS queue subscribed, to read back what was sent."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    with mock_aws():
        topic = boto3.client("sns", region_name=REGION).create_topic(Name="riskplatform-alerts")
        sqs = boto3.client("sqs", region_name=REGION)
        queue = sqs.create_queue(QueueName="inbox")["QueueUrl"]
        queue_arn = sqs.get_queue_attributes(QueueUrl=queue, AttributeNames=["QueueArn"])
        boto3.client("sns", region_name=REGION).subscribe(
            TopicArn=topic["TopicArn"],
            Protocol="sqs",
            Endpoint=queue_arn["Attributes"]["QueueArn"],
        )

        def received() -> list[dict]:
            messages = sqs.receive_message(QueueUrl=queue, MaxNumberOfMessages=10)
            return [json.loads(m["Body"]) for m in messages.get("Messages", [])]

        yield topic["TopicArn"], received


def test_one_email_is_published_with_the_subject_and_body(sns):
    topic_arn, received = sns
    assert send_limit_alerts(_summary(ALERTS), topic_arn) is True
    (message,) = received()
    subject, body = compose_limit_email(_summary(ALERTS))
    assert (message["Subject"], message["Message"]) == (subject, body)


def test_notify_run_reads_the_runs_summary_and_the_environment(sns, tmp_path):
    topic_arn, received = sns
    folder = tmp_path / "limits" / "run-1"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(json.dumps(_summary(ALERTS)), encoding="utf-8")
    assert notify_run(tmp_path, "run-1", env={}) is False  # no topic: nothing sent
    assert received() == []
    assert notify_run(tmp_path, "run-1", env={ALERT_TOPIC_ENV: topic_arn}) is True
    assert len(received()) == 1


# --- Through the pipeline command --------------------------------------------------------------


def _pipeline_args(tmp_path) -> list[str]:
    small = tmp_path / "small.yaml"
    small.write_text(f"base: {(CONFIG / 'generator.yaml').as_posix()}\nn_days: 2\n")
    return ["--config", str(small), "--data", str(tmp_path / "data")]


def test_pipeline_emails_once_with_the_runs_alerts(sns, tmp_path, monkeypatch):
    topic_arn, received = sns
    monkeypatch.setenv(ALERT_TOPIC_ENV, topic_arn)
    code = main(_pipeline_args(tmp_path))

    data = tmp_path / "data"
    run_id = json.loads((data / "validated" / "_run.json").read_text(encoding="utf-8"))["run_id"]
    summary = json.loads((data / "limits" / run_id / "summary.json").read_text(encoding="utf-8"))
    # The 2-day synthetic bank has EVE limit breaches (BANK_UK among them), so an email is due.
    assert summary["alerts"], "precondition: the test bank should have limit alerts"
    (message,) = received()  # exactly one email for the run
    assert code == 2
    sent = [line[2:] for line in message["Message"].splitlines() if line.startswith("- ")]
    assert sent == [describe_alert(alert) for alert in summary["alerts"]]
    assert message["Subject"].startswith(f"riskplatform limits {summary['as_of']}:")


def test_pipeline_without_a_topic_never_asks_for_an_sns_client(tmp_path, monkeypatch):
    monkeypatch.delenv(ALERT_TOPIC_ENV, raising=False)

    def no_aws(*args, **kwargs):
        raise AssertionError("boto3.client should not be called without a topic")

    monkeypatch.setattr(notify.boto3, "client", no_aws)
    assert main(_pipeline_args(tmp_path)) in (0, 2)
