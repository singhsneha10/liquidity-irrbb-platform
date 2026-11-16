"""Dagster orchestration of the daily pipeline: assets, checks, a job and a schedule.

Run the web UI locally with:   dagster dev        (needs: pip install -e ".[orchestration]")
Materialise everything once:   dagster asset materialize --select "*" -m riskplatform.orchestration.definitions

Each stage's output is a Dagster **asset**, so the UI shows lineage, run history and metadata:

    raw_data -> raw_checks -> validated_data -> liquidity_metrics -> limit_status -> daily_changes
                                             -> irrbb_results     ->     -> tableau_extracts -> daily_report -> published_run

Every asset calls the same function as the pipeline command (riskplatform.pipeline), so there
is no second copy of any logic. The rules match the pipeline:

- A stage that stops (a raw check fails, the quality run stops) raises a Dagster Failure with
  its reasons, so the assets after it do not run, and data/validated/ keeps the last good data.
- Findings (a liquidity breach, an IRRBB outlier) are asset checks with WARN severity: visible
  in the UI, but they do not block publishing. The numbers were produced; they need attention.
- Only published_run talks to the network (S3), so only it retries.

The schedule (weekdays 06:00 London) starts switched off; turn it on in the UI. Each scheduled
run generates the business days up to its scheduled date (as --as-of does); runs started by hand
use the config's n_days unless the settings' as_of is set. In AWS the daily
run is a scheduled container running the same functions (Phase 7.4); running Dagster's own
daemon and web server in AWS would cost money around the clock, so Dagster is the orchestration
and monitoring layer locally and for demos.
"""

import json
from pathlib import Path

import dagster as dg

from riskplatform.changes import describe_movement, run_changes
from riskplatform.config import load_storage_config, parse_as_of
from riskplatform.extracts import run_extracts
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.irrbb.run import run_irrbb
from riskplatform.limits import describe_alert, run_limits
from riskplatform.metrics.run import run_metrics
from riskplatform.notify import notify_run
from riskplatform.pipeline import PipelineInputs, load_inputs
from riskplatform.quality.run import run_quality
from riskplatform.report import run_report
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
    limits: str = "config/limits.yaml"
    reporting: str = "config/reporting.yaml"
    data: str = "data"
    # Generate business days up to this date (YYYY-MM-DD or 'today'); empty: the config's
    # n_days. The schedule sets it to each run's scheduled date (see daily_schedule).
    as_of: str | None = None

    def inputs(self) -> PipelineInputs:
        """Every config, loaded and validated (a bad config fails before any data is written)."""
        as_of = parse_as_of(self.as_of) if self.as_of else None
        return load_inputs(
            Path(self.config),
            Path(self.quality),
            Path(self.liquidity),
            Path(self.irrbb),
            as_of,
            limits_path=Path(self.limits),
            reporting_path=Path(self.reporting),
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
    kinds={"python"},
    check_specs=[dg.AssetCheckSpec("no_limit_warning_or_breach", asset="limit_status")],
)
def limit_status(settings: PipelineSettings) -> dg.MaterializeResult:
    """Utilisation and status of every internal limit (config/limits.yaml) for this run."""
    run = run_limits(settings.data_dir, _published_run_id(settings), settings.inputs().limits)
    counts = run.summary["latest_status"]
    return dg.MaterializeResult(
        metadata={
            "as_of": run.summary["as_of"],
            "breaches": counts["breach"],
            "warnings": counts["warning"],
            "alerts": dg.MetadataValue.md(
                "\n".join(f"- {describe_alert(alert)}" for alert in run.alerts) or "None."
            ),
            "path": dg.MetadataValue.path(str(run.out_dir)),
        },
        check_results=[
            dg.AssetCheckResult(
                check_name="no_limit_warning_or_breach",
                passed=not run.alerts,
                severity=WARN,
                metadata={"breaches": counts["breach"], "warnings": counts["warning"]},
            )
        ],
    )


@dg.asset(group_name=GROUP, deps=[limit_status], kinds={"python", "parquet"})
def daily_changes(settings: PipelineSettings) -> dg.MaterializeResult:
    """What changed since the business day before: limit movements and balance drivers."""
    run = run_changes(settings.data_dir, _published_run_id(settings), settings.inputs().reporting)
    summary = run.summary
    moved = summary["limit_movements"]
    return dg.MaterializeResult(
        metadata={
            "latest_date": summary["latest_date"],
            "previous_date": summary["previous_date"] or "none (one day only)",
            **{f"limits_{name}": n for name, n in summary["movements"].items()},
            "limit_movements": dg.MetadataValue.md(
                "\n".join(f"- {describe_movement(item)}" for item in moved) or "None."
            ),
            "path": dg.MetadataValue.path(str(run.out_dir)),
        }
    )


@dg.asset(group_name=GROUP, deps=[daily_changes], kinds={"csv"})
def tableau_extracts(settings: PipelineSettings) -> dg.MaterializeResult:
    """Tableau-ready CSVs of the run, also copied to data/extracts/latest/ (a fixed folder)."""
    run = run_extracts(settings.data_dir, _published_run_id(settings))
    files = run.manifest["files"]
    return dg.MaterializeResult(
        metadata={
            "files": len(files),
            "rows": sum(f["rows"] for f in files.values()),
            "path": dg.MetadataValue.path(str(run.out_dir)),
            "latest": dg.MetadataValue.path(str(run.latest_dir)),
        }
    )


@dg.asset(group_name=GROUP, deps=[tableau_extracts], kinds={"pdf"})
def daily_report(settings: PipelineSettings) -> dg.MaterializeResult:
    """The daily PDF report of the run (riskplatform.report)."""
    run = run_report(settings.data_dir, _published_run_id(settings))
    return dg.MaterializeResult(
        metadata={"pages": run.pages, "path": dg.MetadataValue.path(str(run.path))}
    )


@dg.asset(
    group_name=GROUP,
    deps=[daily_report],
    kinds={"s3"},
    retry_policy=dg.RetryPolicy(max_retries=3, delay=30, backoff=dg.Backoff.EXPONENTIAL),
)
def published_run(settings: PipelineSettings) -> dg.MaterializeResult:
    """The run copied to S3 under its id, latest.json last (only with backend s3)."""
    storage = load_storage_config(settings.storage)
    run_id = _published_run_id(settings)
    if storage.backend != "s3":
        emailed = notify_run(settings.data_dir, run_id)  # only if alerts and a topic is set
        return dg.MaterializeResult(
            metadata={"backend": "local", "run_id": run_id, "limit_email_sent": emailed}
        )
    data = settings.data_dir
    metrics = json.loads((data / "metrics" / run_id / "summary.json").read_text(encoding="utf-8"))
    irrbb = json.loads((data / "irrbb" / run_id / "summary.json").read_text(encoding="utf-8"))
    limits = json.loads((data / "limits" / run_id / "summary.json").read_text(encoding="utf-8"))
    findings = bool(metrics["breaches"] or irrbb["outliers"] or limits["alerts"])
    status, exit_code = ("findings", 2) if findings else ("ok", 0)
    stages = [
        {"name": name, "status": "ok"} for name in ("raw_data", "raw_checks", "validated_data")
    ]
    record = publish_run(make_client(storage), storage, data, run_id, status, exit_code, stages)
    emailed = notify_run(data, run_id)  # after publishing, as the pipeline does
    return dg.MaterializeResult(
        metadata={
            "backend": "s3",
            "run_id": run_id,
            "status": status,
            "files": sum(record["files"].values()),
            "bucket": storage.s3.bucket,
            "limit_email_sent": emailed,
        }
    )


ASSETS = [
    raw_data,
    raw_checks,
    validated_data,
    liquidity_metrics,
    irrbb_results,
    limit_status,
    daily_changes,
    tableau_extracts,
    daily_report,
    published_run,
]

daily_job = dg.define_asset_job("daily_risk_pipeline", selection=dg.AssetSelection.groups(GROUP))


@dg.schedule(
    name="daily_risk_pipeline_schedule",
    job=daily_job,
    cron_schedule="0 6 * * 1-5",  # 06:00 on weekdays
    execution_timezone="Europe/London",
    default_status=dg.DefaultScheduleStatus.STOPPED,  # switched on deliberately, in the UI
)
def daily_schedule(context: dg.ScheduleEvaluationContext) -> dg.RunRequest:
    """Each run covers the business days up to the date it was scheduled for (not the date it
    happens to start, so a late or caught-up run still covers its own day).

    Run config replaces the resource's whole configuration, not one field: fields not given
    fall back to PipelineSettings' class defaults. Definitions below uses exactly those defaults,
    so only as_of changes. Give any other setting here too if Definitions ever stops using them.
    """
    day = context.scheduled_execution_time.date().isoformat()  # in execution_timezone
    return dg.RunRequest(
        run_key=day,  # one run per scheduled day, even if the schedule is evaluated twice
        run_config={"resources": {"settings": {"config": {"as_of": day}}}},
        tags={"as_of": day},
    )


defs = dg.Definitions(
    assets=ASSETS,
    jobs=[daily_job],
    schedules=[daily_schedule],
    resources={"settings": PipelineSettings()},
)
