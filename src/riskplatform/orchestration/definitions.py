"""Dagster orchestration of the daily pipeline: assets, checks, a job and a schedule.

Run the web UI locally with:   dagster dev        (needs: pip install -e ".[orchestration]")
Materialise everything once:   dagster asset materialize --select "*" -m riskplatform.orchestration.definitions

Each stage's output is a Dagster **asset**, so the UI shows lineage, run history and metadata:

    raw_data -> raw_checks -> validated_data -> liquidity_metrics -> published_run
                                             -> irrbb_results     ->

Every asset calls the same function as the pipeline command (riskplatform.pipeline), so there
is no second copy of any logic. The rules match the pipeline:

- A stage that stops (a raw check fails, the quality run stops) raises a Dagster Failure with
  its reasons, so the assets after it do not run, and data/validated/ keeps the last good data.
- Findings (a liquidity breach, an IRRBB outlier) are asset checks with WARN severity: visible
  in the UI, but they do not block publishing. The numbers were produced; they need attention.
- Only published_run talks to the network (S3), so only it retries.

The schedule (weekdays 06:00 London) starts switched off; turn it on in the UI. In AWS the daily
run is a scheduled container running the same functions (Phase 7.4); running Dagster's own
daemon and web server in AWS would cost money around the clock, so Dagster is the orchestration
and monitoring layer locally and for demos.
"""

import json
from pathlib import Path

import dagster as dg

from riskplatform.config import load_storage_config
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.irrbb.run import run_irrbb
from riskplatform.metrics.run import run_metrics
from riskplatform.pipeline import PipelineInputs, load_inputs
from riskplatform.quality.run import run_quality
from riskplatform.storage import make_client, publish_run
from riskplatform.validation.check_raw import run_checks

GROUP = "risk_pipeline"
WARN = dg.AssetCheckSeverity.WARN


class PipelineSettings(dg.ConfigurableResource):
    """Where the configs live and where data is written (the pipeline command's defaults)."""

    config: str = "config/generator.yaml"
    quality: str = "config/quality.yaml"
    liquidity: str = "config/liquidity"
    irrbb: str = "config/irrbb"
    storage: str = "config/storage.yaml"
    data: str = "data"

    def inputs(self) -> PipelineInputs:
        """Every config, loaded and validated (a bad config fails before any data is written)."""
        return load_inputs(
            Path(self.config), Path(self.quality), Path(self.liquidity), Path(self.irrbb)
        )

    @property
    def data_dir(self) -> Path:
        return Path(self.data)


def _published_run_id(settings: PipelineSettings) -> str:
    manifest = settings.data_dir / "validated" / "_run.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["run_id"]


@dg.asset(group_name=GROUP, kinds={"python", "parquet"})
def raw_data(settings: PipelineSettings) -> dg.MaterializeResult:
    """The synthetic bank and market data in data/raw/: positions, rates, FX, control totals."""
    cfg = settings.inputs().generator
    raw = generate(cfg)
    write_outputs(raw, settings.data_dir / "raw")
    return dg.MaterializeResult(
        metadata={
            "position_rows": len(raw.positions),
            "days": cfg.n_days,
            "rates_source": cfg.rates.source,
            "path": dg.MetadataValue.path(str(settings.data_dir / "raw")),
        }
    )


@dg.asset(group_name=GROUP, deps=[raw_data], kinds={"duckdb"})
def raw_checks(settings: PipelineSettings) -> dg.MaterializeResult:
    """Independent SQL checks of the generated files; any failure stops the run."""
    results = run_checks(settings.data_dir / "raw", settings.inputs().generator)
    failed = [result.name for result in results if not result.passed]
    if failed:
        raise dg.Failure(description=f"raw checks failed: {', '.join(failed)}")
    return dg.MaterializeResult(
        metadata={
            "checks_passed": sum(not r.skipped for r in results),
            "checks_skipped": sum(r.skipped for r in results),
        }
    )


@dg.asset(group_name=GROUP, deps=[raw_checks], kinds={"pandera"})
def validated_data(settings: PipelineSettings) -> dg.MaterializeResult:
    """data/validated/: rows that passed the quality layer; a stopped run publishes nothing."""
    inputs = settings.inputs()
    run = run_quality(
        settings.data_dir / "raw", settings.data_dir, inputs.generator, inputs.quality
    )
    report = run.run_dir / "breaks_report.md"
    if run.status != "published":
        raise dg.Failure(
            description=f"quality run {run.run_id} stopped: "
            + "; ".join(run.report["stop_reasons"]),
            metadata={"run_id": run.run_id, "breaks_report": dg.MetadataValue.path(str(report))},
        )
    return dg.MaterializeResult(
        metadata={
            "run_id": run.run_id,
            "rows_quarantined": run.report["rows"]["quarantined"],
            "breaks_report": dg.MetadataValue.path(str(report)),
        }
    )


@dg.asset(
    group_name=GROUP,
    deps=[validated_data],
    kinds={"python"},
    check_specs=[dg.AssetCheckSpec("no_liquidity_breach", asset="liquidity_metrics")],
)
def liquidity_metrics(settings: PipelineSettings) -> dg.MaterializeResult:
    """LCR, NSFR and the maturity ladder (Basel and UK PRA), per entity-currency, entity, group."""
    inputs = settings.inputs()
    run = run_metrics(
        settings.data_dir / "validated",
        settings.data_dir / "metrics",
        inputs.generator,
        inputs.liquidity,
        inputs.ladder,
    )
    breaches = run.summary["breaches"]
    return dg.MaterializeResult(
        metadata={"breaches": len(breaches), "path": dg.MetadataValue.path(str(run.out_dir))},
        check_results=[
            dg.AssetCheckResult(
                check_name="no_liquidity_breach",
                passed=not breaches,
                severity=WARN,
                metadata={"breaches": len(breaches)},
            )
        ],
    )


@dg.asset(
    group_name=GROUP,
    deps=[validated_data],
    kinds={"python"},
    check_specs=[dg.AssetCheckSpec("no_irrbb_outlier", asset="irrbb_results")],
)
def irrbb_results(settings: PipelineSettings) -> dg.MaterializeResult:
    """Delta EVE, the outlier test, delta NII and the repricing gap (Basel and UK PRA)."""
    inputs = settings.inputs()
    run = run_irrbb(
        settings.data_dir / "validated", settings.data_dir / "irrbb", inputs.generator, inputs.irrbb
    )
    flagged = run.summary["outliers"]
    return dg.MaterializeResult(
        metadata={"outlier_results": len(flagged), "path": dg.MetadataValue.path(str(run.out_dir))},
        check_results=[
            dg.AssetCheckResult(
                check_name="no_irrbb_outlier",
                passed=not flagged,
                severity=WARN,
                metadata={"outlier_results": len(flagged)},
            )
        ],
    )


@dg.asset(
    group_name=GROUP,
    deps=[liquidity_metrics, irrbb_results],
    kinds={"s3"},
    retry_policy=dg.RetryPolicy(max_retries=3, delay=30, backoff=dg.Backoff.EXPONENTIAL),
)
def published_run(settings: PipelineSettings) -> dg.MaterializeResult:
    """The run copied to S3 under its id, latest.json last (only with backend s3)."""
    storage = load_storage_config(settings.storage)
    run_id = _published_run_id(settings)
    if storage.backend != "s3":
        return dg.MaterializeResult(metadata={"backend": "local", "run_id": run_id})
    data = settings.data_dir
    metrics = json.loads((data / "metrics" / run_id / "summary.json").read_text(encoding="utf-8"))
    irrbb = json.loads((data / "irrbb" / run_id / "summary.json").read_text(encoding="utf-8"))
    findings = bool(metrics["breaches"] or irrbb["outliers"])
    status, exit_code = ("findings", 2) if findings else ("ok", 0)
    stages = [
        {"name": name, "status": "ok"} for name in ("raw_data", "raw_checks", "validated_data")
    ]
    record = publish_run(make_client(storage), storage, data, run_id, status, exit_code, stages)
    return dg.MaterializeResult(
        metadata={
            "backend": "s3",
            "run_id": run_id,
            "status": status,
            "files": sum(record["files"].values()),
            "bucket": storage.s3.bucket,
        }
    )


ASSETS = [raw_data, raw_checks, validated_data, liquidity_metrics, irrbb_results, published_run]

daily_job = dg.define_asset_job("daily_risk_pipeline", selection=dg.AssetSelection.groups(GROUP))

daily_schedule = dg.ScheduleDefinition(
    job=daily_job,
    cron_schedule="0 6 * * 1-5",  # 06:00 on weekdays
    execution_timezone="Europe/London",
    default_status=dg.DefaultScheduleStatus.STOPPED,  # switched on deliberately, in the UI
)

defs = dg.Definitions(
    assets=ASSETS,
    jobs=[daily_job],
    schedules=[daily_schedule],
    resources={"settings": PipelineSettings()},
)
