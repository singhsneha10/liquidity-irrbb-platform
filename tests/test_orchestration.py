"""Dagster orchestration: definitions, lineage, a full run, stops block what follows, publishing."""

import json
from datetime import UTC, datetime
from pathlib import Path

import boto3
import dagster as dg
import pytest
from moto import mock_aws

from riskplatform.orchestration import definitions
from riskplatform.orchestration.definitions import ASSETS, PipelineSettings, daily_schedule, defs

ROOT = Path(__file__).resolve().parents[1]
ORDER = [
    "raw_data",
    "raw_checks",
    "validated_data",
    "liquidity_metrics",
    "irrbb_results",
    "published_run",
]
BUCKET, REGION = "riskplatform-test", "eu-west-2"


@pytest.fixture
def settings(tmp_path) -> PipelineSettings:
    """The real configs with a 2-day bank, writing under tmp_path."""
    small = tmp_path / "small.yaml"
    small.write_text(f"base: {(ROOT / 'config' / 'generator.yaml').as_posix()}\nn_days: 2\n")
    config = ROOT / "config"
    return PipelineSettings(
        config=str(small),
        quality=str(config / "quality.yaml"),
        liquidity=str(config / "liquidity"),
        irrbb=str(config / "irrbb"),
        storage=str(config / "storage.yaml"),
        data=str(tmp_path / "data"),
    )


def _run(settings) -> dg.ExecuteInProcessResult:
    return dg.materialize(ASSETS, resources={"settings": settings}, raise_on_error=False)


def _materialized(result) -> set[str]:
    return {event.asset_key.to_user_string() for event in result.get_asset_materialization_events()}


def test_definitions_load_with_the_job_and_a_switched_off_schedule():
    keys = {key.to_user_string() for key in defs.resolve_all_asset_keys()}
    assert keys == set(ORDER)
    job = defs.resolve_job_def("daily_risk_pipeline")
    assert {key.to_user_string() for key in job.asset_layer.selected_asset_keys} == set(ORDER)
    schedule = defs.resolve_schedule_def("daily_risk_pipeline_schedule")
    assert (schedule.cron_schedule, schedule.execution_timezone) == ("0 6 * * 1-5", "Europe/London")
    assert schedule.default_status == dg.DefaultScheduleStatus.STOPPED


def test_lineage_follows_the_pipeline_order():
    graph = defs.resolve_asset_graph()
    parents = {
        key.to_user_string(): {p.to_user_string() for p in graph.get(key).parent_keys}
        for key in graph.get_all_asset_keys()
    }
    assert parents == {
        "raw_data": set(),
        "raw_checks": {"raw_data"},
        "validated_data": {"raw_checks"},
        "liquidity_metrics": {"validated_data"},
        "irrbb_results": {"validated_data"},
        "published_run": {"liquidity_metrics", "irrbb_results"},
    }


def test_a_full_run_materializes_every_asset_and_reports_the_checks(settings):
    result = _run(settings)
    assert result.success
    assert _materialized(result) == set(ORDER)
    run_id = json.loads((Path(settings.data) / "validated" / "_run.json").read_text())["run_id"]
    validated = result.asset_materializations_for_node("validated_data")[0]
    assert validated.metadata["run_id"].value == run_id
    checks = {c.check_name: c for c in result.get_asset_check_evaluations()}
    assert set(checks) == {"no_liquidity_breach", "no_irrbb_outlier"}
    assert all(
        c.severity == dg.AssetCheckSeverity.WARN for c in checks.values()
    )  # findings never block
    published = result.asset_materializations_for_node("published_run")[0]
    assert published.metadata["backend"].value == "local"


def test_a_quality_stop_blocks_everything_after_it(settings, monkeypatch):
    write = definitions.write_outputs

    def write_without_control_totals(raw, raw_dir):
        write(raw, raw_dir)
        (Path(raw_dir) / "control_totals.json").unlink()

    monkeypatch.setattr(definitions, "write_outputs", write_without_control_totals)
    result = _run(settings)
    assert not result.success
    assert _materialized(result) == {"raw_data", "raw_checks"}
    failure = next(e for e in result.all_events if e.is_step_failure)
    assert "control totals" in failure.step_failure_data.error.message


def test_a_raw_check_failure_stops_everything_after_it(settings, monkeypatch):
    from riskplatform.validation.check_raw import CheckResult

    monkeypatch.setattr(definitions, "run_checks", lambda *_: [CheckResult("planted", failures=1)])
    result = _run(settings)
    assert not result.success
    assert _materialized(result) == {"raw_data"}


def test_published_run_uploads_to_s3_when_the_backend_is_s3(settings, monkeypatch):
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("RISKPLATFORM_STORAGE_BACKEND", "s3")
    monkeypatch.setenv("RISKPLATFORM_S3_BUCKET", BUCKET)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
        )
        result = _run(settings)
        latest = json.loads(
            client.get_object(Bucket=BUCKET, Key="riskplatform/latest.json")["Body"].read()
        )
    assert result.success
    published = result.asset_materializations_for_node("published_run")[0]
    assert latest["run_id"] == published.metadata["run_id"].value
    assert published.metadata["backend"].value == "s3"


def test_schedule_runs_up_to_its_scheduled_date():
    """A late or caught-up run still covers its own day: the date comes from the schedule."""
    friday = datetime(2026, 11, 13, 6, 0, tzinfo=UTC)
    request = daily_schedule(dg.build_schedule_context(scheduled_execution_time=friday))
    assert request.run_key == "2026-11-13"  # one run per scheduled day
    assert request.run_config == {"resources": {"settings": {"config": {"as_of": "2026-11-13"}}}}
    assert request.tags["as_of"] == "2026-11-13"


def test_as_of_setting_sets_the_days_of_every_asset(settings):
    """Mon 3 Aug to Wed 5 Aug: three business days, from raw data to IRRBB."""
    # A new resource, not model_copy: Dagster keeps a resource's config apart from the copy.
    fields = {
        name: getattr(settings, name)
        for name in ("quality", "liquidity", "irrbb", "storage", "data")
    }
    full = PipelineSettings(
        **fields, config=str(ROOT / "config" / "generator.yaml"), as_of="2026-08-05"
    )
    result = _run(full)
    assert result.success
    data = Path(full.data)
    run_id = json.loads((data / "validated" / "_run.json").read_text())["run_id"]
    raw = result.asset_materializations_for_node("raw_data")[0]
    assert raw.metadata["days"].value == 3
    assert len(list((data / "validated" / "positions").glob("*.parquet"))) == 3
    irrbb = json.loads((data / "irrbb" / run_id / "summary.json").read_text())
    assert irrbb["dates"] == 3
