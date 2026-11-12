"""The whole daily pipeline in one command: generate -> raw checks -> quality -> metrics -> IRRBB.

Run with:  python -m riskplatform.pipeline          (or: riskplatform-run, or: make run)
           python -m riskplatform.pipeline --config config/generator_real.yaml

Each stage calls the same function as its own command, so the pipeline adds no calculation of
its own. Stages run in order; a stage that stops ends the pipeline and later stages do not run.
data/validated/ then keeps the last good data (the quality run guarantees that), so metrics and
IRRBB are never calculated on data that failed its checks.

Exit codes, so a scheduler can tell a broken run from a run that found something:
  0  every stage ran; no liquidity minimum breached and no IRRBB outlier
  1  the pipeline stopped: a raw check failed, the quality run stopped, or a stage raised an
     error (Python exits with 1 on an unhandled error too)
  2  every stage ran, but a liquidity minimum was breached or an entity or the group is an
     IRRBB outlier. The numbers were produced; they need attention, not a rerun.

All outputs go under --data (default data/): raw/, validated/, quality_runs/, metrics/, irrbb/,
and logs/pipeline-<UTC time>.log, the full log of each run (an error's traceback included).
"""

import argparse
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from riskplatform.config import (
    GeneratorConfig,
    IrrbbRules,
    LadderConfig,
    LiquidityRules,
    QualityConfig,
    load_generator_config,
    load_ladder_config,
    load_quality_config,
)
from riskplatform.ingestion.generate import generate, write_outputs
from riskplatform.irrbb.run import load_irrbb_regimes, run_irrbb
from riskplatform.logs import log_to_file, setup_logging
from riskplatform.metrics.run import load_liquidity_regimes, run_metrics
from riskplatform.quality.run import run_quality
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


def load_inputs(
    config: Path, quality: Path, liquidity_dir: Path, irrbb_dir: Path
) -> PipelineInputs:
    """Load and validate every config first, so a bad config fails before any data is written."""
    return PipelineInputs(
        generator=load_generator_config(config),
        quality=load_quality_config(quality),
        liquidity=load_liquidity_regimes(liquidity_dir),
        ladder=load_ladder_config(liquidity_dir / "ladder.yaml"),
        irrbb=load_irrbb_regimes(irrbb_dir),
    )


Step = Callable[[], tuple[StageStatus, str]]


def run_pipeline(inputs: PipelineInputs, data_dir: str | Path) -> PipelineResult:
    """Run every stage in order under data_dir, stopping at the first stage that stops."""
    data_dir = Path(data_dir)
    raw_dir, validated_dir = data_dir / "raw", data_dir / "validated"
    cfg = inputs.generator

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

    steps: list[tuple[str, Step]] = [
        ("generate", generate_step),
        ("raw_checks", raw_checks_step),
        ("quality", quality_step),
        ("metrics", metrics_step),
        ("irrbb", irrbb_step),
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
    return PipelineResult(stages)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the whole pipeline in one command.")
    parser.add_argument("--config", type=Path, default=Path("config/generator.yaml"))
    parser.add_argument("--quality", type=Path, default=Path("config/quality.yaml"))
    parser.add_argument("--liquidity", type=Path, default=Path("config/liquidity"))
    parser.add_argument("--irrbb", type=Path, default=Path("config/irrbb"))
    parser.add_argument("--data", type=Path, default=Path("data"))
    args = parser.parse_args(argv)

    setup_logging()
    inputs = load_inputs(args.config, args.quality, args.liquidity, args.irrbb)
    log_path = args.data / "logs" / f"pipeline-{datetime.now(UTC):%Y%m%dT%H%M%SZ}.log"
    with log_to_file(log_path):
        logger.info("Pipeline starting with %s; log file %s", args.config, log_path)
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
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
