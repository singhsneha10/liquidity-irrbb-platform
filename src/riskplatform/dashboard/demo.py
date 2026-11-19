"""Demo data for the public dashboard: the default synthetic bank, built with the real pipeline.

The public dashboard (Phase 10, Streamlit Community Cloud) has no AWS access and no secrets, so
it does not read the daily AWS results. Instead it runs the same pipeline the tests and the AWS
task run (generate -> raw checks -> quality -> metrics -> IRRBB -> limits -> changes -> early
warning -> extracts -> report) on the default configs, once per server start, into a temporary
folder, and shows that run. Every number is the pipeline's own; only where it ran differs.

Measured on 2026-11-19 (60 business days): 19 to 50 seconds and a peak of 249 MB, inside the
host's smallest allowance (690 MB). Labelled choice: the series is the configs' fixed default
(to 2026-10-23), not up to today, so the demo always shows the same, documented numbers.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from riskplatform.pipeline import load_inputs, run_pipeline

logger = logging.getLogger(__name__)

DEMO_ENV = "RISKPLATFORM_DEMO"  # "1": the dashboard builds and shows the demo run


class DemoBuildError(Exception):
    """The demo pipeline run stopped, so there is nothing to show."""


@dataclass(frozen=True)
class DemoRun:
    data_dir: Path
    run_id: str
    status: str  # "ok" or "findings" (the synthetic bank has findings: BANK_UK's EVE breach)
    built_at: datetime


def build_demo(root: str | Path, data_dir: str | Path) -> DemoRun:
    """Run the whole pipeline on the default configs under root/config, writing to data_dir."""
    config = Path(root) / "config"
    inputs = load_inputs(
        config / "generator.yaml",
        config / "quality.yaml",
        config / "liquidity",
        config / "irrbb",
        limits_path=config / "limits.yaml",
        reporting_path=config / "reporting.yaml",
        early_warning_path=config / "early_warning.yaml",
    )
    logger.info("Building the demo run (%d business days) in %s", inputs.generator.n_days, data_dir)
    result = run_pipeline(inputs, data_dir)
    if result.status == "stopped" or result.run_id is None:
        stopped = [f"{s.name}: {s.detail}" for s in result.stages if s.status == "stopped"]
        raise DemoBuildError(f"the demo pipeline run stopped: {'; '.join(stopped)}")
    return DemoRun(Path(data_dir), result.run_id, result.status, datetime.now(UTC))
