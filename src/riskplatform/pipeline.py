"""The whole daily pipeline in one command: generate -> raw checks -> quality -> metrics ->
IRRBB -> limits -> changes -> early warning -> extracts -> report.

Run with:  python -m riskplatform.pipeline          (or: riskplatform-run, or: make run)
           python -m riskplatform.pipeline --config config/generator_real.yaml
           python -m riskplatform.pipeline --as-of today          (what the daily AWS task runs)

--as-of DATE: generate every business day from start_date up to DATE (YYYY-MM-DD, or 'today'
in UTC), so each scheduled run adds that day to the series; the earlier days stay identical.
Without it, the config's n_days (60), so local runs, tests and CI are always the same.

Each stage calls the same function as its own command, so the pipeline adds no calculation of
its own. Stages run in order; a stage that stops ends the pipeline and later stages do not run.
data/validated/ then keeps the last good data (the quality run guarantees that), so metrics and
IRRBB are never calculated on data that failed its checks.

Exit codes, so a scheduler can tell a broken run from a run that found something:
  0  every stage ran; no liquidity minimum breached, no IRRBB outlier, every internal limit ok
  1  the pipeline stopped: a raw check failed, the quality run stopped, or a stage raised an
     error (Python exits with 1 on an unhandled error too)
  2  every stage ran, but a liquidity minimum was breached, an entity or the group is an
     IRRBB outlier, or an internal limit (config/limits.yaml) is at warning or breach on the
     latest date. The numbers were produced; they need attention, not a rerun.

The early_warning stage measures two indicators set in config/early_warning.yaml (retail
deposit outflow over 4 weeks, and the run-off at which each LCR would reach its limit;
riskplatform.early_warning); an indicator in alert is a finding (exit 2).

The changes stage compares the latest day with the one before (riskplatform.changes, "what
changed since yesterday"); it reports, so it is always ok and never changes the exit code. So
do the extracts stage (Tableau-ready CSVs, riskplatform.extracts) and the report stage (the
daily PDF, riskplatform.report).

All outputs go under --data (default data/): raw/, validated/, quality_runs/, metrics/, irrbb/,
limits/, changes/, early_warning/, extracts/, reports/ and logs/pipeline-<UTC time>.log, the full log of each run (an error's traceback included).
With backend s3 in --storage (config/storage.yaml, or RISKPLATFORM_STORAGE_BACKEND=s3), the
run's outputs are then also copied to S3 under its run id (riskplatform.storage). Then, if any
internal limit is at warning or breach and RISKPLATFORM_ALERT_TOPIC_ARN is set (in AWS), one
limit alert email is sent through SNS (riskplatform.notify), with what changed since the day
before; otherwise the alerts stay logged.
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from riskplatform.changes import run_changes
from riskplatform.config import (
    AS_OF_HELP,
    EarlyWarningConfig,
    GeneratorConfig,
    IrrbbRules,
    LadderConfig,
    LimitsConfig,
    LiquidityRules,
    QualityConfig,
    ReportingConfig,
    StorageConfig,
    load_early_warning_config,
    load_generator_config,
    load_ladder_config,
    load_limits_config,
    load_quality_config,
    load_reporting_config,
    load_storage_config,
    parse_as_of,
)
from riskplatform.early_warning import describe_indicator, run_early_warning
from riskplatform.extracts import run_extracts
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.irrbb.run import load_irrbb_regimes, run_irrbb
from riskplatform.limits import describe_alert, run_limits
from riskplatform.logs import log_to_file, setup_logging
from riskplatform.metrics.run import load_liquidity_regimes, run_metrics
from riskplatform.notify import notify_run
from riskplatform.quality.run import run_quality
from riskplatform.report import run_report
from riskplatform.storage import make_client, publish_run
from riskplatform.validation.check_raw import run_checks

logger = logging.getLogger(__name__)

StageStatus = Literal["ok", "findings", "stopped"]
EXIT_CODES = {"ok": 0, "stopped": 1, "findings": 2}


@dataclass(frozen=True)
class StageResult:
    name: str
    status: StageStatus
    detail: str  # one line for the log
    seconds: float


@dataclass(frozen=True)
class PipelineResult:
    stages: list[StageResult]
    run_id: str | None = None  # the quality run's id, once that stage has run

    @property
    def status(self) -> StageStatus:
        """stopped if any stage stopped, else findings if any stage found something, else ok."""
        statuses = {stage.status for stage in self.stages}
        for status in ("stopped", "findings"):
            if status in statuses:
                return status
        return "ok"

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]


@dataclass(frozen=True)
class PipelineInputs:
    """Every validated config the stages need, loaded once before anything runs."""

    generator: GeneratorConfig
    quality: QualityConfig
    liquidity: dict[str, LiquidityRules]
    ladder: LadderConfig
    irrbb: dict[str, IrrbbRules]
    limits: LimitsConfig
    reporting: ReportingConfig
    early_warning: EarlyWarningConfig


def load_inputs(
    config: Path,
    quality: Path,
    liquidity_dir: Path,
    irrbb_dir: Path,
    as_of: date | None = None,
    limits_path: Path = Path("config/limits.yaml"),
    reporting_path: Path = Path("config/reporting.yaml"),
    early_warning_path: Path = Path("config/early_warning.yaml"),
) -> PipelineInputs:
    """Load and validate every config first, so a bad config fails before any data is written.

    With as_of, the bank covers every business day from start_date up to it (one day count for
    every stage of the run); without it, the config's own n_days.
    """
    return PipelineInputs(
        generator=load_generator_config(config, as_of),
        quality=load_quality_config(quality),
        liquidity=load_liquidity_regimes(liquidity_dir),
        ladder=load_ladder_config(liquidity_dir / "ladder.yaml"),
        irrbb=load_irrbb_regimes(irrbb_dir),
        limits=load_limits_config(limits_path),
        reporting=load_reporting_config(reporting_path),
        early_warning=load_early_warning_config(early_warning_path),
    )


Step = Callable[[], tuple[StageStatus, str]]


def run_pipeline(inputs: PipelineInputs, data_dir: str | Path) -> PipelineResult:
    """Run every stage in order under data_dir, stopping at the first stage that stops."""
    data_dir = Path(data_dir)
    raw_dir, validated_dir = data_dir / "raw", data_dir / "validated"
    cfg = inputs.generator
    seen: dict[str, str] = {}  # the quality run's id, recorded by quality_step

    def generate_step() -> tuple[StageStatus, str]:
        raw = generate(cfg)
        write_outputs(raw, raw_dir)
        return "ok", f"{len(raw.positions):,} position rows over {cfg.n_days} days to {raw_dir}"

    def raw_checks_step() -> tuple[StageStatus, str]:
        results = run_checks(raw_dir, cfg)
        failed = [result.name for result in results if not result.passed]
        if failed:
            return "stopped", f"failed: {', '.join(failed)}"
        return "ok", f"{sum(not r.skipped for r in results)} checks passed"

    def quality_step() -> tuple[StageStatus, str]:
        run = run_quality(raw_dir, data_dir, cfg, inputs.quality)
        seen["run_id"] = run.run_id
        report = run.run_dir / "breaks_report.md"
        if run.status != "published":
            return "stopped", f"run {run.run_id} stopped: {'; '.join(run.report['stop_reasons'])}"
        quarantined = run.report["rows"]["quarantined"]
        return "ok", f"run {run.run_id} published; {quarantined} rows quarantined; {report}"

    def metrics_step() -> tuple[StageStatus, str]:
        run = run_metrics(validated_dir, data_dir / "metrics", cfg, inputs.liquidity, inputs.ladder)
        breaches = run.summary["breaches"]
        return ("findings" if breaches else "ok"), f"{len(breaches)} breaches; {run.out_dir}"

    def irrbb_step() -> tuple[StageStatus, str]:
        run = run_irrbb(validated_dir, data_dir / "irrbb", cfg, inputs.irrbb)
        flagged = run.summary["outliers"]
        return ("findings" if flagged else "ok"), f"{len(flagged)} outlier results; {run.out_dir}"

    def limits_step() -> tuple[StageStatus, str]:
        run = run_limits(data_dir, seen["run_id"], inputs.limits)
        for alert in run.alerts:
            logger.warning("Limit %s", describe_alert(alert))
        counts = run.summary["latest_status"]
        detail = (
            f"{counts['breach']} breaches, {counts['warning']} warnings on {run.summary['as_of']}"
        )
        return ("findings" if run.alerts else "ok"), f"{detail}; {run.out_dir}"

    def changes_step() -> tuple[StageStatus, str]:
        run = run_changes(data_dir, seen["run_id"], inputs.reporting)
        summary = run.summary
        if summary["previous_date"] is None:
            return "ok", f"one day only, nothing to compare; {run.out_dir}"
        moved = ", ".join(f"{n} {name}" for name, n in summary["movements"].items() if n)
        since = f"{summary['latest_date']} against {summary['previous_date']}"
        return "ok", f"limits {moved or 'none'} ({since}); {run.out_dir}"

    def early_warning_step() -> tuple[StageStatus, str]:
        run = run_early_warning(
            data_dir, seen["run_id"], cfg, inputs.liquidity,
            inputs.limits.limits["lcr"].value, inputs.early_warning,
        )  # fmt: skip
        for alert in run.alerts:
            logger.warning("Early warning %s", describe_indicator(alert))
        detail = f"{len(run.alerts)} indicators in alert on {run.summary['as_of']}"
        return ("findings" if run.alerts else "ok"), f"{detail}; {run.out_dir}"

    def extracts_step() -> tuple[StageStatus, str]:
        run = run_extracts(data_dir, seen["run_id"])
        return "ok", f"{len(run.manifest['files'])} CSV files; {run.out_dir} (and {run.latest_dir})"

    def report_step() -> tuple[StageStatus, str]:
        run = run_report(data_dir, seen["run_id"])
        return "ok", f"{run.pages} pages; {run.path}"

    steps: list[tuple[str, Step]] = [
        ("generate", generate_step),
        ("raw_checks", raw_checks_step),
        ("quality", quality_step),
        ("metrics", metrics_step),
        ("irrbb", irrbb_step),
        ("limits", limits_step),
        ("changes", changes_step),
        ("early_warning", early_warning_step),
        ("extracts", extracts_step),
        ("report", report_step),
    ]
    stages: list[StageResult] = []
    for name, step in steps:
        started = time.perf_counter()
        logger.info("Stage %s: starting", name)
        status, detail = step()
        stage = StageResult(name, status, detail, time.perf_counter() - started)
        stages.append(stage)
        log = logger.error if status == "stopped" else logger.info
        log("Stage %s: %s in %.1fs (%s)", name, status.upper(), stage.seconds, detail)
        if status == "stopped":
            break
    return PipelineResult(stages, run_id=seen.get("run_id"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the whole pipeline in one command.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--quality", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--liquidity", type=Path, default=Path("config/liquidity"))
    parser.add_argument("--irrbb", type=Path, default=Path("config/irrbb"))
    parser.add_argument("--limits", type=Path, default=Path("config/limits.yaml"))
    parser.add_argument("--reporting", type=Path, default=Path("config/reporting.yaml"))
    parser.add_argument("--early-warning", type=Path, default=Path("config/early_warning.yaml"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--storage", type=Path, default=Path("config/storage.yaml"))
    parser.add_argument("--as-of", type=parse_as_of, help=AS_OF_HELP)
    args = parser.parse_args(argv)

    setup_logging()
    inputs = load_inputs(
        args.config,
        args.quality,
        args.liquidity,
        args.irrbb,
        args.as_of,
        args.limits,
        args.reporting,
        args.early_warning,
    )
    storage = load_storage_config(args.storage)  # checked before any data is written
    log_path = args.data / "logs" / f"pipeline-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.log"
    with log_to_file(log_path):
        bank = inputs.generator
        logger.info(
            "Pipeline starting with %s (as of %s: %d business days from %s); log file %s",
            args.config,
            args.as_of or "config n_days",
            bank.n_days,
            bank.start_date,
            log_path,
        )
        try:
            result = run_pipeline(inputs, args.data)
        except Exception:
            logger.exception("Pipeline STOPPED by an error (exit code 1)")
            raise
        logger.info(
            "Pipeline %s (exit code %d): %s",
            result.status.upper(),
            result.exit_code,
            ", ".join(f"{stage.name} {stage.status}" for stage in result.stages),
        )
    if storage.backend == "s3":  # after the log file is complete, so it is published whole
        publish(result, storage, args.data, log_path)
    if any(stage.name == "limits" for stage in result.stages):  # after publishing, so the
        notify_run(args.data, result.run_id)  # email's S3 location exists; sends only if alerts
    return result.exit_code


def publish(result: PipelineResult, storage: StorageConfig, data_dir: Path, log_path: Path) -> None:
    """Copy the run's outputs to S3; a run that stopped before the quality stage has none."""
    if result.run_id is None:
        logger.warning("Nothing to publish: the pipeline stopped before the quality stage")
        return
    stages = [
        {"name": s.name, "status": s.status, "detail": s.detail, "seconds": round(s.seconds, 2)}
        for s in result.stages
    ]
    publish_run(
        make_client(storage),
        storage,
        data_dir,
        result.run_id,
        result.status,
        result.exit_code,
        stages,
        log_file=log_path,
    )


if __name__ == "__main__":
    sys.exit(main())
